const { app, BrowserWindow, dialog, shell } = require('electron');
const packageInfo = require('./package.json');
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const net = require('net');
const updateToken = require('crypto').randomBytes(32).toString('hex');

app.setName(packageInfo.build?.productName || '活动抽奖系统');

// Only allow one desktop instance to use the shared SQLite database. Without
// this guard, opening the EXE twice starts two Python servers against the same
// database and SQLite can block reads/writes for its full busy timeout.
const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });
}

let serverProcess = null;
let mainWindow = null;
let localPort = null;
let isQuitting = false;
let quitGuard = null;
const trace = (...args) => {
  if (process.env.LUCKY_DRAW_TRACE === '1') console.error('[lucky-draw]', ...args);
};
// 主窗口成功显示过 = 应用已经真正跑起来了。只有在这之前才允许「GPU 挂了就换软件渲染
// 重启一次」，避免现场运行中因为一次驱动抖动把正在用的大屏重启掉。
let bootCompleted = false;
let softwareRendering =
  process.argv.includes('--in-process-gpu') ||
  process.argv.includes('--disable-gpu') ||
  process.env.LUCKY_DRAW_SOFTWARE_RENDERING === '1';

// 关闭本地服务。
//
// PyInstaller --onefile 打出来的程序其实是两层进程：外层 bootloader 先把运行时
// 解包到 %TEMP%\_MEIxxxx，再启动内层的「真身」。Node 的 child.kill() 只发得到
// 外层，内层会变成孤儿进程 —— 它还占着端口、还连着同一个 SQLite，现场表现为
// 「程序已经关了，任务管理器里却还剩一个 lucky-draw-server.exe」。
// 所以 Windows 上必须用 taskkill /T 把整棵进程树一起收掉。
//
// 这里刻意用同步调用：退出路径上异步 spawn 的 taskkill 有可能还没执行完，
// 主进程就先退出了，孤儿依旧会留下。
function stopLocalServer() {
  const child = serverProcess;
  if (!child || child.__stopped) return;
  child.__stopped = true;

  // 外壳已经退出的话，绝对不能再按这个 PID 发 taskkill：Windows 会回收并用
  // 这个 PID 给别的进程，/T /F 会把那个无辜进程连它的子进程一起杀掉。
  // 这种情况（外壳先走、内层还活着）交给服务端自己的父进程监视兜底。
  if (child.exitCode !== null || child.signalCode !== null) return;

  if (process.platform === 'win32' && child.pid) {
    try {
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
        windowsHide: true,
        stdio: 'ignore',
        timeout: 5000,
      });
      return;
    } catch (_) {
      // taskkill 不可用时落到下面的普通 kill 兜底
    }
  }
  try {
    child.kill('SIGTERM');
  } catch (_) {
    // 进程可能已经退出了
  }
}

// 只保留最近的一段输出：服务端每处理一个请求都会往 stderr 打一行日志，
// 无限累积会一直涨内存（现场要连开两天）。
let serverStartupError = '';
function recordServerOutput(chunk) {
  serverStartupError = (serverStartupError + chunk.toString()).slice(-4000);
}

function appRoot() {
  return app.isPackaged ? path.join(process.resourcesPath, 'app') : __dirname;
}

function pythonCommand(root) {
  if (!app.isPackaged) {
    return process.env.LUCKY_DRAW_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  }
  return process.platform === 'win32'
    ? path.join(root, 'runtime', 'python', 'python.exe')
    : path.join(root, 'runtime', 'python', 'bin', 'python3');
}

function serverLaunch(root) {
  const python = pythonCommand(root);
  const pythonArgs = ['-u', path.join(root, 'server.py')];
  const binary = path.join(
    root,
    'runtime',
    process.platform === 'win32' ? 'lucky-draw-server.exe' : 'lucky-draw-server',
  );

  // 开发模式直接用本机 Python。
  if (!app.isPackaged) return { command: python, args: pythonArgs };

  // 正式安装包必须优先用 PyInstaller 打出来的服务程序：
  // `runtime/` 在 .gitignore 里，CI 只会 `stage:*-server` 产出这一个 exe，
  // 那个带嵌入式 Python 的 runtime/python 只存在于本机手工 stage 的构建里。
  // 之前反过来优先找 runtime/python，导致从 Release 下载安装的版本
  // spawn 失败、只报一句「本地服务启动超时」。
  const hasBinary = fs.existsSync(binary);
  const hasPython = fs.existsSync(python);

  // 万一某些应用控制策略拦截 PyInstaller 单文件程序，可以显式回退：
  // 设置 LUCKY_DRAW_USE_SERVER_BINARY=0 强制使用内置 Python（前提是它存在）。
  if (process.env.LUCKY_DRAW_USE_SERVER_BINARY === '0' && hasPython) {
    return { command: python, args: pythonArgs };
  }
  if (hasBinary) return { command: binary, args: [] };
  if (hasPython) return { command: python, args: pythonArgs };
  return { command: binary, args: [], missing: binary };
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
  });
}

// 允许显式钉住端口（LUCKY_DRAW_PORT=4173）。默认仍然是让系统随机分配一个空闲端口，
// 只有在明确设置了这个变量时才用它 —— 打包后的自动验证脚本（tools/packaged-smoke）
// 就是靠它来确定该去哪个端口做健康检查的。钉住的端口被占用时服务会启动失败，
// 这属于「显式指定就要显式承担」，比静默换端口更容易排查。
function resolveListenPort() {
  const pinned = Number.parseInt(process.env.LUCKY_DRAW_PORT || '', 10);
  if (Number.isInteger(pinned) && pinned > 0 && pinned < 65536) {
    return Promise.resolve(pinned);
  }
  return findFreePort();
}

function startLocalServer(port) {
  const root = appRoot();
  const launch = serverLaunch(root);
  const dataDir = process.env.LUCKY_DRAW_DATA_DIR || path.join(app.getPath('userData'), 'data');
  fs.mkdirSync(dataDir, { recursive: true });

  if (launch.missing) {
    throw new Error(
      '内置服务程序缺失，安装包不完整。\n\n期望的文件：\n' +
        launch.missing +
        '\n\n请重新下载安装完整版本。',
    );
  }

  serverStartupError = '';
  const child = spawn(launch.command, launch.args, {
    cwd: root,
    windowsHide: process.platform === 'win32',
    env: {
      ...process.env,
      LUCKY_DRAW_PORT: String(port),
      LUCKY_DRAW_DIST_DIR: path.join(root, 'dist'),
      LUCKY_DRAW_DATA_DIR: dataDir,
      LUCKY_DRAW_UPDATE_TOKEN: updateToken,
      // 把自己的 PID 告诉服务端，让它能直接监视「应用主进程」而不是只盯着
      // 中间那层 PyInstaller bootloader。见 server.py 的 watch_parent_process()：
      // macOS/Linux 上主进程被 kill -9 时 bootloader 不会跟着死，只靠 getppid()
      // 永远发现不了孤儿。
      LUCKY_DRAW_PARENT_PID: String(process.pid)
    }
  });
  serverProcess = child;

  child.stderr.on('data', recordServerOutput);
  // 必须消费 stdout，否则管道写满会把服务端卡住。
  child.stdout.on('data', () => {});
  child.on('error', error => {
    serverStartupError = `${error.message}\n试图启动：${launch.command}`;
  });
  child.on('exit', (code, signal) => {
    // 主动关闭服务时不要报错：进程以非 0 码退出是 taskkill 的正常结果。
    if (isQuitting || child.__stopped) return;
    // 信号导致的退出没有退出码，但同样是异常退出，要如实告诉用户。
    if ((code || signal) && mainWindow && !mainWindow.isDestroyed()) {
      // 注意：showErrorBox 是同步阻塞 API。绝不能在退出流程里调用它 ——
      // 那会把主进程卡在 Not Responding 上永远退不出去（实测踩过）。
      dialog.showErrorBox(
        '本地服务启动失败',
        serverStartupError || `服务进程已退出（${code || signal}）`,
      );
    }
  });
  return serverProcess;
}

async function waitForServer(port) {
  const url = `http://127.0.0.1:${port}/api/health`;
  // PyInstaller 单文件程序首次启动要把运行时解包到临时目录，慢的时候要好几秒，
  // 所以这里给到 15 秒，并且一旦进程真的挂了就立刻报错、不再空等到超时。
  for (let i = 0; i < 150; i += 1) {
    if (serverProcess && serverProcess.pid === undefined && serverStartupError) {
      throw new Error(`本地服务无法启动\n\n${serverStartupError}`);
    }
    if (serverProcess && serverProcess.exitCode !== null) {
      throw new Error(
        `本地服务已退出（代码 ${serverProcess.exitCode}）` +
          (serverStartupError ? `\n\n${serverStartupError}` : ''),
      );
    }
    try {
      const response = await fetch(url);
      if (response.ok) {
        serverStartupError = '';
        return;
      }
    } catch (_) {
      // The Python server may need a moment to initialize its SQLite database.
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(
    '本地服务启动超时' + (serverStartupError ? `\n\n${serverStartupError}` : ''),
  );
}

function createWindow(port) {
  trace('creating main window', port);
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1080,
    minHeight: 700,
    show: false,
    backgroundColor: '#f3f7f4',
    icon: path.join(appRoot(), 'build', 'icon.ico'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(appRoot(), 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      devTools: !app.isPackaged
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(`http://127.0.0.1:${port}/`)) {
      // 大屏子窗口（window.open 打开）默认会带 File/Edit/View/Window 菜单栏，
      // 投影上很难看。autoHideMenuBar 隐藏它、但保留 F11 等加速键。
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          autoHideMenuBar: true,
          backgroundColor: '#C10043',
          icon: path.join(appRoot(), 'build', 'icon.ico')
        }
      };
    }
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.once('ready-to-show', () => {
    trace('main window ready to show');
    bootCompleted = true;
    mainWindow.show();
  });
  mainWindow.webContents.on('did-fail-load', (_event, code, description, url) => {
    trace('page failed to load', code, description, url);
  });
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    trace('renderer process gone', details.reason, details.exitCode);
  });
  mainWindow.loadURL(`http://127.0.0.1:${port}/?session=1`);
  mainWindow.on('closed', () => {
    trace('main window closed');
    mainWindow = null;
    // 主窗口就是应用本体，关掉它等于结束使用。
    // 只靠 window-all-closed 的话，之前用 window.open 打开的大屏窗口还开着时
    // 应用不会退出 —— 用户以为已经关掉了程序，任务管理器里却还有进程和端口。
    app.quit();
  });
}

async function boot() {
  trace('starting application');
  localPort = await resolveListenPort();
  trace('using local port', localPort);
  startLocalServer(localPort);
  await waitForServer(localPort);
  trace('local service ready');
  createWindow(localPort);
  require('./updates.cjs').registerUpdates(() => mainWindow, () => localPort, updateToken);
}

if (hasSingleInstanceLock) {
  app.whenReady().then(() => {
    trace('Electron ready');
    return boot();
  }).catch(error => {
    trace('application startup failed', error.message);
    dialog.showErrorBox('应用启动失败', error.message);
    app.quit();
  });

  app.on('before-quit', () => {
    trace('before quit');
    isQuitting = true;
    stopLocalServer();
    // 兜底：万一还有别的东西挡着退出（同步对话框、没关掉的管道等），
    // 给几秒宽限后强制退出。用户已经按了关闭，不能因为清理不干净就赖着不走。
    if (!quitGuard) quitGuard = setTimeout(() => app.exit(0), 4000);
  });

  // before-quit 在某些路径下（例如 app.exit 之外被别处打断）不一定跑全，
  // will-quit 这里再收一次尾，确保服务进程不会漏。
  app.on('will-quit', () => {
    trace('will quit');
    isQuitting = true;
    stopLocalServer();
    if (quitGuard) {
      clearTimeout(quitGuard);
      quitGuard = null;
    }
  });

  app.on('window-all-closed', () => {
    trace('all windows closed');
    if (process.platform !== 'darwin') app.quit();
  });

  // 虚拟机、远程桌面、驱动异常的机器上，GPU 进程根本起不来。实测（Windows 11 虚拟机）：
  // Chromium 会连续重试 5 次，然后在窗口显示出来之前直接
  // 「FATAL: GPU process isn't usable. Goodbye.」退出，而且退出码 3 是 C 层 _exit，
  // 连 Node 的 exit 钩子都不跑 —— 现场表现就是「双击了、什么都没发生」。
  //
  // 好消息是 FATAL 之前 Chromium 一定会先派发 child-process-gone(type=GPU)，
  // 所以这里还有机会自救：换一组「软渲染」开关重启一次，至少让应用能用。
  // （实测这台虚拟机里事件比 FATAL 早约 30ms 到达，而且连发了 10 次。）
  //
  // 重启时带哪几个开关很关键，实测过一轮（Windows 11 无显示适配器的虚拟机）：
  //   · 什么都不带 —— 照旧 FATAL；
  //   · 只带 --disable-gpu —— **照样 FATAL**。它只是关掉硬件加速，Chromium 仍然要
  //     为显示合成器单独起一个 GPU 进程，那个进程起不来一样会「GPU process isn't
  //     usable」；这是上一版自愈实际无效的原因；
  //   · 只带 --in-process-gpu —— 存活，服务与页面都正常；
  //   · --disable-gpu + --in-process-gpu —— 同样存活。
  // 所以真正起作用的是 --in-process-gpu（把 GPU 线程并进浏览器进程，没有独立的
  // GPU 进程可崩），--disable-gpu 作为附加保险一起带上。
  let gpuFailures = 0;
  app.on('child-process-gone', (_event, details) => {
    if (details.type !== 'GPU' || bootCompleted || softwareRendering) return;
    // 攒够两次再动手：好机器上偶发一次 GPU 进程重启不该把整个应用重启掉，
    // 而坏机器上 FATAL 之前会有 5~10 次，多等一个完全来得及。
    gpuFailures += 1;
    if (gpuFailures < 2) return;
    softwareRendering = true; // 只自救一次，避免「重启→又失败→再重启」死循环
    console.warn('[gpu] GPU 进程不可用，改用软件渲染重新启动');
    // 必须先把单实例锁交出去，否则新实例拿不到锁会立刻自杀。
    try {
      app.releaseSingleInstanceLock();
    } catch (_) {
      // 老版本 Electron 没有这个方法时忽略，最多是重启失败
    }
    try {
      app.relaunch({
        args: process.argv.slice(1).concat(['--disable-gpu', '--in-process-gpu']),
      });
    } catch (_) {
      return; // 重启都失败了，那就照常走原来的错误流程
    }
    // 这里必须硬退，不能 app.quit()：GPU 已经坏了，优雅退出流程可能迟迟走不完。
    // 退出前一定要先 releaseSingleInstanceLock()，否则新实例启动时锁还没放开。
    // app.exit 跳过 before-quit，本地服务交给它自己的父进程监视兜底（1 秒内自退）。
    app.exit(0);
  });

  app.on('activate', () => {
    if (!mainWindow && localPort) createWindow(localPort);
  });
}
