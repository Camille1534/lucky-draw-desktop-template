#!/usr/bin/env node
'use strict';

/**
 * 打包产物冒烟验证（跨平台：Windows / macOS）。
 *
 * 为什么需要它：v1.0.5 发布出去之后才发现——从 Release 下载安装的版本「双击没有任何
 * 反应」，根因是安装包里少了一个文件（`runtime/lucky-draw-server.exe` 没打进去），
 * 而应用启动失败只报了一句「本地服务启动超时」，光看日志根本定位不到。这类「打包
 * 落差」问题（本地目录里能跑、装机版缺件）必须在发版前自动拦下来，于是有了这个脚本。
 *
 * 三层检查，逐层收窄：
 *
 *   1. static —— 拆开安装包，看关键文件在不在、有没有把开发机上的垃圾目录打进去。
 *      这一层直接对应上面那个 bug：少 server 程序 = 必挂。
 *   2. server —— 直接启动打好的服务程序，打 /api/health，关掉后确认不留残留进程。
 *   3. app    —— 真正启动应用，等它把本地服务拉起来（/api/health 通），然后**硬杀**
 *      应用进程树，确认服务端能在 1~2 秒内自退、端口释放。这一层验证的是
 *      「主进程被强杀也不留孤儿」这个修复（PyInstaller onefile 的内层进程很容易变孤儿）。
 *
 * 用法：
 *   node tools/packaged-smoke/verify_packaged.js --app release/win-unpacked --mode both
 *   node tools/packaged-smoke/verify_packaged.js --app "release/mac/活动抽奖系统.app" --mode both --require-app
 *
 * 退出码：0 = 通过（可能有警告）；1 = 有硬失败。
 *
 * 关于 FAIL 与 WARN 的边界（刻意设计成这样，CI 才不会因为环境抖动卡发布）：
 *   · static / server 两层的失败一律算 FAIL —— 这两层不依赖图形界面，结果确定。
 *   · app 层默认只报 WARN：CI 的 runner（尤其 Linux/容器化 macOS）经常没有可用的
 *     窗口系统，Electron 起来就会以「GPU 进程不可用」自杀，这属于环境限制而不是
 *     产品缺陷。加 --require-app 可以把它升级成 FAIL，在开发机上自查时更严格。
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const { spawn, spawnSync } = require('child_process');

const isWindows = process.platform === 'win32';
const SERVER_BIN_NAME = isWindows ? 'lucky-draw-server.exe' : 'lucky-draw-server';

// 有些开发机 / 自动化宿主会把 ELECTRON_RUN_AS_NODE=1 写进全局环境变量。它一存在，
// Electron 就退化成「纯 Node」：不给脚本参数时它会安静地读 stdin 然后退出（表现就是
// 「双击了、什么都没发生、也没有任何日志」），而带上任何 --开关 都会被 Node 的选项
// 解析器拒掉，报 `bad option: --disable-gpu`。
// 这纯粹是宿主环境问题、不是产品缺陷，但会让验证脚本给出假警报，所以在派生被测进程
// 之前必须把它摘掉。NODE_OPTIONS 同理，避免宿主的 Node 选项串进被测应用。
function sanitizedEnv(extra) {
  const env = { ...process.env, ...extra };
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.NODE_OPTIONS;
  return env;
}

// 应用起不来时，只要 stderr 里出现这些字样，就判定成「环境不支持图形界面」而不是产品缺陷。
const ENVIRONMENT_NOISE = [
  'gpu process isn\'t usable',
  'gpu process is not usable',
  'render-process-gone',
  'err_failed',
  'failed to connect to the bus',
  'cannot open display',
  'no protocol specified',
  'missing x server',
  'libgl error',
  'dri3',
  'swiftshader',
  'failed to create gbm',
  'bad option:',
];

// ------------------------------------------------------------------ 结果收集
const results = [];
function record(level, scope, message) {
  results.push({ level, scope, message });
  const tag = { PASS: 'PASS', FAIL: 'FAIL', WARN: 'WARN', INFO: 'INFO' }[level];
  console.log(`[${tag}] (${scope}) ${message}`);
}
const pass = (scope, m) => record('PASS', scope, m);
const fail = (scope, m) => record('FAIL', scope, m);
const warn = (scope, m) => record('WARN', scope, m);
const info = (scope, m) => record('INFO', scope, m);

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ------------------------------------------------------------------ 参数
function parseArgs(argv) {
  const opts = { app: null, mode: 'both', requireApp: false, help: false, noAppArgs: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--app') opts.app = argv[(i += 1)];
    else if (arg === '--mode') opts.mode = argv[(i += 1)];
    else if (arg === '--require-app') opts.requireApp = true;
    else if (arg === '--no-app-args') opts.noAppArgs = true;
    else if (arg === '--help' || arg === '-h') opts.help = true;
    else throw new Error(`未知参数：${arg}`);
  }
  if (!['static', 'server', 'app', 'both'].includes(opts.mode)) {
    throw new Error(`--mode 只能是 static | server | app | both，收到：${opts.mode}`);
  }
  return opts;
}

// ------------------------------------------------------------------ 路径解析
// electron-builder 打包时会把 package.json 里的 build 段剥掉，所以**不能**靠读
// 打包后的 package.json 来拿 productName —— 拿不到。必须老老实实去找可执行文件。
function resolveAppRoot(appPath) {
  const abs = path.resolve(appPath);
  if (!fs.existsSync(abs)) throw new Error(`路径不存在：${abs}`);

  const candidates = [
    path.join(abs, 'resources', 'app'), // win-unpacked / mac 里的 Resources/app
    path.join(abs, 'Contents', 'Resources', 'app'), // 直接指向 .app 外层
    abs, // 直接指向 resources/app
  ];
  for (const dir of candidates) {
    if (fs.existsSync(path.join(dir, 'main.cjs'))) return dir;
  }
  throw new Error(`在 ${abs} 下找不到 resources/app（缺少 main.cjs）`);
}

function resolveAppExecutable(appPath) {
  const abs = path.resolve(appPath);

  if (abs.endsWith('.app') || fs.existsSync(path.join(abs, 'Contents', 'Info.plist'))) {
    const macosDir = path.join(abs, 'Contents', 'MacOS');
    if (!fs.existsSync(macosDir)) throw new Error(`找不到 ${macosDir}`);
    const plist = path.join(abs, 'Contents', 'Info.plist');
    if (fs.existsSync(plist)) {
      const raw = fs.readFileSync(plist, 'utf8');
      const m = raw.match(/<key>CFBundleExecutable<\/key>\s*<string>([^<]+)<\/string>/);
      if (m) {
        const exe = path.join(macosDir, m[1]);
        if (fs.existsSync(exe)) return exe;
      }
    }
    const entries = fs.readdirSync(macosDir).filter(name => !name.startsWith('.'));
    if (entries.length === 1) return path.join(macosDir, entries[0]);
    throw new Error(`无法在 ${macosDir} 中确定可执行文件`);
  }

  const exes = fs
    .readdirSync(abs)
    .filter(name => name.toLowerCase().endsWith('.exe'))
    .map(name => path.join(abs, name))
    .filter(p => fs.statSync(p).isFile())
    .sort((a, b) => fs.statSync(b).size - fs.statSync(a).size);

  if (exes.length === 0) throw new Error(`在 ${abs} 下找不到任何 .exe`);
  return exes[0];
}

function dirSizeBytes(target) {
  let total = 0;
  const stack = [target];
  while (stack.length) {
    const current = stack.pop();
    let stat;
    try {
      stat = fs.lstatSync(current);
    } catch (_) {
      continue;
    }
    if (stat.isSymbolicLink()) continue;
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(current)) stack.push(path.join(current, entry));
    } else {
      total += stat.size;
    }
  }
  return total;
}

const mb = bytes => `${(bytes / 1024 / 1024).toFixed(1)} MB`;

// ------------------------------------------------------------------ 进程工具
// 列出当前系统里所有「镜像名匹配」的进程 PID。用来做残留检测：只在「启动之前不存在、
// 关闭之后还在」的情况下才报残留，避免把开发机上用户自己开着的实例误判成我们的锅。
function listPidsByImage(imageName) {
  const pids = new Set();
  try {
    if (isWindows) {
      const out = spawnSync('tasklist', ['/FI', `IMAGENAME eq ${imageName}`, '/FO', 'CSV', '/NH'], {
        encoding: 'utf8',
        windowsHide: true,
        timeout: 15000,
      });
      for (const line of (out.stdout || '').split('\n')) {
        const m = line.match(/"([^"]+)"\s*,\s*"(\d+)"/);
        // tasklist already filters the exact image name. Its localized output
        // may be GBK rather than UTF-8, so only parse the ASCII PID here.
        if (m) pids.add(Number(m[2]));
      }
    } else {
      const out = spawnSync('ps', ['-Ao', 'pid=,comm='], { encoding: 'utf8', timeout: 15000 });
      for (const line of (out.stdout || '').split('\n')) {
        const m = line.trim().match(/^(\d+)\s+(.+)$/);
        if (!m) continue;
        if (path.basename(m[2]).toLowerCase() === imageName.toLowerCase()) pids.add(Number(m[1]));
      }
    }
  } catch (_) {
    // 拿不到进程列表时不阻塞验证，只当没有残留。
  }
  return pids;
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === 'EPERM';
  }
}

// 硬杀整棵进程树。刻意用最强硬的方式（Windows: taskkill /T /F；POSIX: SIGKILL），
// 因为要验证的正是「主进程被非正常干掉时服务端还能不能自己收尾」。
function killTree(pid) {
  try {
    if (isWindows) {
      spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
        windowsHide: true,
        stdio: 'ignore',
        timeout: 15000,
      });
    } else {
      try {
        process.kill(pid, 'SIGKILL');
      } catch (_) {
        /* 已经退出了 */
      }
    }
  } catch (_) {
    /* 忽略 */
  }
}

// ------------------------------------------------------------------ 网络工具
function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
  });
}

function portInUse(port) {
  return new Promise(resolve => {
    const probe = net.createServer();
    probe.once('error', () => resolve(true));
    probe.listen(port, '127.0.0.1', () => probe.close(() => resolve(false)));
  });
}

async function healthOk(port) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) return false;
    const body = await response.json().catch(() => ({}));
    return body && body.ok !== false;
  } catch (_) {
    return false;
  }
}

async function waitForHealth(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await healthOk(port)) return true;
    await sleep(150);
  }
  return false;
}

// ------------------------------------------------------------------ 静态检查
function checkStatic(appRoot) {
  const required = [
    ['main.cjs', 'Electron 主进程入口'],
    ['package.json', '应用元数据'],
    ['server.py', '本地服务源码（打包后仍需随包提供）'],
    ['dist/index.html', '界面（单文件）'],
  ];
  for (const [rel, label] of required) {
    const target = path.join(appRoot, rel);
    if (fs.existsSync(target)) pass('static', `存在 ${rel}（${label}）`);
    else fail('static', `缺少 ${rel}（${label}）`);
  }

  const fontsDir = path.join(appRoot, 'dist', 'fonts', 'poppins');
  if (fs.existsSync(fontsDir)) {
    const woff2 = fs.readdirSync(fontsDir).filter(f => f.endsWith('.woff2'));
    if (woff2.length >= 8) pass('static', `Poppins 字体齐全（${woff2.length} 个 woff2）`);
    else warn('static', `Poppins 字体偏少，只有 ${woff2.length} 个 woff2`);
  } else {
    fail('static', '缺少 dist/fonts/poppins（字体静默回退，界面外观会变）');
  }

  const serverBin = path.join(appRoot, 'runtime', SERVER_BIN_NAME);
  if (fs.existsSync(serverBin)) pass('static', `存在 ${SERVER_BIN_NAME}（${mb(fs.statSync(serverBin).size)}）`);
  else fail('static', `缺少 runtime/${SERVER_BIN_NAME} —— 装机版会「双击没反应」，就是 v1.0.5 那个 bug`);

  // 开发机上的嵌入式 Python / 备份目录必须被 build.files 排除掉，否则安装包会莫名
  // 胀到几百 MB。这里只警告：它不影响功能。
  for (const junk of ['python', 'python-full-backup', '_excluded']) {
    const target = path.join(appRoot, 'runtime', junk);
    if (fs.existsSync(target)) {
      warn('static', `runtime/${junk} 被打进了安装包（${mb(dirSizeBytes(target))}），检查 package.json 的 files 排除规则`);
    }
  }

  const total = dirSizeBytes(appRoot);
  if (total > 60 * 1024 * 1024) warn('static', `resources/app 体积偏大：${mb(total)}`);
  else info('static', `resources/app 体积：${mb(total)}`);
}

// ------------------------------------------------------------------ 服务程序模式
async function checkServer(appRoot) {
  const serverBin = path.join(appRoot, 'runtime', SERVER_BIN_NAME);
  if (!fs.existsSync(serverBin)) {
    info('server', '服务程序不存在，跳过（已由 static 层报出）');
    return;
  }

  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'lucky-draw-smoke-'));
  const distDir = path.join(appRoot, 'dist');
  const port = await freePort();
  const before = listPidsByImage(SERVER_BIN_NAME);

  const child = spawn(serverBin, [], {
    cwd: appRoot,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: sanitizedEnv({
      LUCKY_DRAW_PORT: String(port),
      LUCKY_DRAW_DIST_DIR: distDir,
      LUCKY_DRAW_DATA_DIR: dataDir,
    }),
  });
  let stderr = '';
  child.stderr.on('data', chunk => {
    stderr = (stderr + chunk.toString()).slice(-2000);
  });
  child.stdout.on('data', () => {});

  const up = await waitForHealth(port, 20000);
  if (up) pass('server', `独立启动服务程序，/api/health 正常（端口 ${port}）`);
  else fail('server', `服务程序启动后 /api/health 不通。stderr：${stderr.trim() || '(空)'}`);

  killTree(child.pid);
  await sleep(1200);

  const after = listPidsByImage(SERVER_BIN_NAME);
  const leaked = [...after].filter(pid => !before.has(pid));
  if (leaked.length === 0) pass('server', '关闭后没有残留服务进程');
  else fail('server', `关闭后仍有残留服务进程：${leaked.join(', ')}`);

  if (await portInUse(port)) fail('server', `关闭后端口 ${port} 仍被占用`);
  else pass('server', `关闭后端口 ${port} 已释放`);

  try {
    fs.rmSync(dataDir, { recursive: true, force: true });
  } catch (_) {
    /* 临时目录清不掉不影响结论 */
  }
}

// ------------------------------------------------------------------ 应用模式
function looksLikeEnvironmentFailure(text) {
  const lower = text.toLowerCase();
  return ENVIRONMENT_NOISE.some(needle => lower.includes(needle));
}

async function checkApp(appPath, opts) {
  const exe = resolveAppExecutable(appPath);
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'lucky-draw-appsmoke-'));
  const port = await freePort();

  const beforeServer = listPidsByImage(SERVER_BIN_NAME);
  const beforeApp = listPidsByImage(path.basename(exe));

  // 默认带 --in-process-gpu 启动。这一层要验的是「启动链路 + 进程清理」，不是渲染
  // 性能；CI runner 和虚拟机普遍没有可用的显示适配器，实测这种情况下 Chromium 会在
  // 窗口出现之前 FATAL「GPU process isn't usable」直接退出。
  // 注意：只加 --disable-gpu 是**没用的** —— 它只关硬件加速，Chromium 仍要为显示
  // 合成器单独起 GPU 进程，那个进程起不来照样 FATAL。真正管用的是 --in-process-gpu
  // （把 GPU 线程并进浏览器进程）。用 --no-app-args 可以不附加任何开关，
  // 那时应用会走自己的「GPU 自愈重启」路径，脚本会自动接管重启后的新实例。
  const appImage = path.basename(exe);
  const appArgs = [...(opts.noAppArgs ? [] : ['--in-process-gpu']), `--user-data-dir=${dataDir}`];
  info('app', `启动 ${exe}${appArgs.length ? `（${appArgs.join(' ')}）` : ''}`);
  const child = spawn(exe, appArgs, {
    cwd: path.dirname(exe),
    windowsHide: false,
    detached: !isWindows,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: sanitizedEnv({
      // 允许应用钉住端口（见 main.cjs 的 resolveListenPort），验证脚本才知道该查哪个端口。
      LUCKY_DRAW_PORT: String(port),
      LUCKY_DRAW_DATA_DIR: dataDir,
      ELECTRON_ENABLE_LOGGING: '1',
    }),
  });
  let output = '';
  const collect = chunk => {
    output = (output + chunk.toString()).slice(-4000);
  };
  child.stdout.on('data', collect);
  child.stderr.on('data', collect);
  child.on('error', error => {
    output += `\n[spawn error] ${error.message}`;
  });

  const up = await waitForHealth(port, 30000);

  // 应用相关的进程（对比启动前的基线，避免把开发机上已有的实例算进来）。
  // 注意这里必须按「集合」而不是按「单个 PID」处理：Electron 是多进程的，
  // GPU / 渲染 / 工具进程都用同一个可执行文件，一个正常运行的应用本来就有好几个同名 PID。
  // 所以「存活」= 集合非空；「强杀应用」= 把这个集合连同最初拉起的进程一起干掉。
  const liveAppPids = () => [...listPidsByImage(appImage)].filter(pid => !beforeApp.has(pid));

  // 我们直接拉起的那个进程已经退出、但仍有同名进程活着 —— 说明应用走了 GPU 自愈：
  // release 单实例锁 → app.relaunch(...) → app.exit(0)（见 main.cjs 的 child-process-gone）。
  const directChildGone = child.exitCode !== null || child.signalCode !== null;
  const selfHealed = directChildGone && liveAppPids().length > 0;
  if (selfHealed) {
    info('app', `原实例已退出，接管 GPU 自愈重启后的新实例：${liveAppPids().join(', ')}`);
  }

  const appAlive = liveAppPids().length > 0;

  if (up) {
    // 服务通了不代表应用真的活着：应用挂掉后，它派生的本地服务要等父进程监视线程
    // 反应过来（约 1 秒）才会自退，这一小段窗口里 /api/health 仍然是通的。
    // 所以必须确认「应用进程也还在」，否则就是假通过。
    if (appAlive) {
      pass('app', `应用启动后本地服务可用（端口 ${port}）`);
      pass('app', '/api/health 返回正常');
      if (selfHealed) pass('app', 'GPU 自愈重启链可用：重启后的实例正常提供服务');
    } else {
      const detail = output.trim().split('\n').slice(-6).join(' | ') || '(无输出)';
      const level = opts.requireApp ? fail : warn;
      level('app', `本地服务通了，但应用进程已经不在 —— 界面其实起不来。输出：${detail}`);
    }
  } else {
    const envIssue = looksLikeEnvironmentFailure(output);
    const detail = output.trim().split('\n').slice(-6).join(' | ') || '(无输出)';
    if (envIssue) {
      warn('app', `应用未能拉起服务，输出像是环境不支持图形界面：${detail}`);
    } else if (opts.requireApp) {
      fail('app', `应用启动后本地服务始终不可用。输出：${detail}`);
    } else {
      warn('app', `应用启动后本地服务始终不可用（本次不作为硬失败）。输出：${detail}`);
    }
  }

  // 关键一步：硬杀应用（模拟「任务管理器结束任务」/ Force Quit），验证服务端不留孤儿。
  const killTargets = [...new Set([...liveAppPids(), ...(pidAlive(child.pid) ? [child.pid] : [])])];
  if (killTargets.length > 0) {
    for (const pid of killTargets) killTree(pid);
    await sleep(2500);

    const leakedServer = [...listPidsByImage(SERVER_BIN_NAME)].filter(pid => !beforeServer.has(pid));
    const leakedApp = liveAppPids();

    if (leakedServer.length === 0) pass('app', '主进程被强杀后，本地服务自行退出，没有留孤儿进程');
    else fail('app', `主进程被强杀后仍有孤儿服务进程：${leakedServer.join(', ')}`);

    if (leakedApp.length === 0) pass('app', '应用进程已完全退出');
    else warn('app', `应用进程仍在：${leakedApp.join(', ')}`);

    if (await portInUse(port)) warn('app', `端口 ${port} 仍被占用`);
    else pass('app', `端口 ${port} 已释放`);
  } else {
    info('app', '应用进程已提前退出，跳过孤儿进程检查');
  }

  try {
    fs.rmSync(dataDir, { recursive: true, force: true });
  } catch (_) {
    /* 忽略 */
  }
}

// ------------------------------------------------------------------ 主流程
async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help || !opts.app) {
    console.log(
      '用法：node tools/packaged-smoke/verify_packaged.js --app <打包目录或 .app> ' +
        '[--mode static|server|app|both] [--require-app] [--no-app-args]',
    );
    process.exit(opts.help ? 0 : 2);
  }

  const appRoot = resolveAppRoot(opts.app);
  console.log(`应用目录：${path.resolve(opts.app)}`);
  console.log(`资源目录：${appRoot}`);
  console.log(`平台：${process.platform} / 验证模式：${opts.mode}`);
  if (process.env.ELECTRON_RUN_AS_NODE) {
    console.log('提示：当前环境设置了 ELECTRON_RUN_AS_NODE，已在派生被测进程时自动摘除');
  }
  console.log('');

  const runStatic = opts.mode === 'static' || opts.mode === 'both';
  const runServer = opts.mode === 'server' || opts.mode === 'both';
  const runApp = opts.mode === 'app' || opts.mode === 'both';

  if (runStatic) checkStatic(appRoot);
  if (runServer) await checkServer(appRoot);
  if (runApp) await checkApp(opts.app, opts);

  const failures = results.filter(r => r.level === 'FAIL');
  const warnings = results.filter(r => r.level === 'WARN');
  console.log('');
  if (failures.length) {
    console.log(`结果：不通过（${failures.length} 个失败，${warnings.length} 个警告）`);
    for (const item of failures) console.log(`  · [${item.scope}] ${item.message}`);
    process.exit(1);
  }
  console.log(`结果：通过（${warnings.length} 个警告）`);
  for (const item of warnings) console.log(`  · [${item.scope}] ${item.message}`);
  process.exit(0);
}

main().catch(error => {
  console.error(`验证脚本自身出错：${error && error.stack ? error.stack : error}`);
  process.exit(1);
});
