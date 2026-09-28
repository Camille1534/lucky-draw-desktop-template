# 可配置活动抽奖系统模板

这是不包含任何真实品牌、嘉宾或活动资料的桌面抽奖模板。Electron 负责独立窗口和打包，Python 本地服务负责抽奖逻辑、Excel 导入与 SQLite 数据库。最终用户不需要打开浏览器，也不需要单独安装 Python。

## 新品牌只需改这里

1. 复制这一整个模板文件夹，或在 GitHub 用它创建一个新仓库。
2. 打开根目录的 `brand.config.json`，填写品牌名称、活动名称、颜色、App ID 和版本号。
3. 将以下默认素材替换为该品牌素材（文件名可不变）：
   - `dist/assets/brand-logo.svg`：后台和大屏 Logo；
   - `dist/assets/event-title.svg`：大屏活动标题；
   - `dist/assets/screen-background.svg`：大屏背景；
   - `build/icon.ico`、`build/icon.png`、`build/icon.svg`：Windows/macOS 应用图标。
4. 运行 `npm run brand:apply`，把配置同步到网页和安装包名称。
5. 在后台导入该活动的嘉宾名单并设置奖品；模板本身不内置真实嘉宾资料。

`appId` 请为每个品牌设置唯一值，例如 `com.acme.eventdraw`，否则同一台电脑安装不同品牌时可能互相覆盖数据目录。

## 开发运行

```powershell
npm install
npm.cmd start
```

开发模式会使用电脑已安装的 `python`（Windows）或 `python3`（macOS）。也可以通过环境变量 `LUCKY_DRAW_PYTHON` 指定 Python 路径。

## 打包 Windows 应用

```powershell
npm run dist
```

打包完成后，`release` 文件夹中会生成：

- `活动抽奖系统 1.0.0.exe`：便携版，双击即可运行
- `活动抽奖系统 Setup 1.0.0.exe`：安装版（NSIS）

应用数据保存在 Windows 用户目录下的 `活动抽奖系统/data`，不会写入安装目录，卸载应用时也不会自动删除抽奖数据。

默认后台密码：`admin123`。

## 跨平台发布

Windows 与 macOS 共用这套 UI、抽奖逻辑和 SQLite 结构。发布时分别打包：

- Windows：NSIS 安装包与便携版 EXE
- macOS：DMG（Apple Silicon / Intel 各一份）

两个平台都用 [PyInstaller](https://pyinstaller.org) 把 `server.py` 打成一个**内置服务程序**
（Windows 是 `runtime/lucky-draw-server.exe`，macOS 是 `runtime/lucky-draw-server`），
由 `main.cjs` 启动。用户机器上不需要装 Python。

`runtime/` 属于构建产物，不提交到 Git；CI 在打包前用 `npm run stage:win-server` /
`npm run stage:mac-server` 现场生成。

### 打包后会自动验证

`.github/workflows/build-desktop.yml`（推 `main` 触发）和 `release.yml`（推 `v*` 标签触发）
都会在打包完成后跑 `tools/packaged-smoke/verify_packaged.js`，在**真正的产物**上检查：

1. 内置服务程序在不在包里、`dist/` 与字体全不全；
2. 服务程序能不能在当前系统/架构上跑起来并响应 `/api/health`；
3. 启动整个应用、确认它**确实活着**并拉起了服务，硬杀之后不留残留进程。

```bash
node tools/packaged-smoke/verify_packaged.js --app release/win-unpacked --mode both
node tools/packaged-smoke/verify_packaged.js --app "release/mac-arm64/活动抽奖系统.app" --mode both
```

可选参数：

- `--require-app`：把 app 层的失败升级为硬失败（开发机自查时更严格）。
- `--no-app-args`：不给应用附加任何开关，走它自己的「GPU 自愈重启」路径，
  脚本会自动接管重启后的新实例并验证它同样正常提供服务。

> 这一步是为了拦住「打包落差」类问题：源码里跑得好好的，装到用户机器上却起不来
> （v1.0.4 的安装包就是这么坏的 —— `runtime/` 被 gitignore，CI 只 stage 了服务程序，
> 而当时的 `main.cjs` 却优先去找 `runtime/python/python.exe`）。
> macOS 我们手上没有机器，这个冒烟测试也是唯一能证明 Mach-O 真能执行的手段。
>
> app 层默认带 `--in-process-gpu` 启动。CI runner 与虚拟机通常没有可用的显示适配器，
> 实测这种情况下 Chromium 会在窗口出现之前直接
> `FATAL: GPU process isn't usable. Goodbye.` 退出。注意 **只加 `--disable-gpu` 是没用的**
> —— 它只关掉硬件加速，Chromium 仍然要为显示合成器单独起 GPU 进程，那个进程起不来照样
> FATAL；真正管用的是 `--in-process-gpu`（把 GPU 线程并进浏览器进程）。`main.cjs` 的
> GPU 自愈重启用的也是这一组开关。

## 首次打开说明（发给现场同事）

### Windows

安装包与便携版都**没有代码签名**，首次运行可能出现「Windows 已保护你的电脑」：

1. 点「更多信息」→「仍要运行」。

如果机器开启了 Windows 11 的**智能应用控制（Smart App Control）**，它会直接拦截未签名程序
且**没有「仍要运行」入口**，只能关闭该功能或改用已签名的版本。

### macOS

桌面版 DMG 目前是 **ad-hoc 签名、未公证**（没有 Apple Developer 证书），
macOS 会提示「无法验证开发者」。做法：

1. **不要直接双击**。右键（或 Control + 点击）应用 → 选「打开」→ 再点一次「打开」。
2. 如果 macOS 15 (Sequoia) 及以后不再给出这个入口：打开
   **系统设置 → 隐私与安全性**，在下方找到被拦下的应用，点「仍要打开」。
3. 万一提示「应用已损坏，无法打开」（通常是从浏览器下载带了隔离属性），执行一次：

   ```bash
   xattr -cr "/Applications/活动抽奖系统.app"
   ```

4. 若用命令行启动，注意 zsh 里 `open` 要带完整路径。

> 要彻底免掉这些提示，只有一条路：Apple Developer Program（99 美元/年）→ 导出
> "Developer ID Application" 证书 → 配到 CI 的 `CSC_LINK` / `CSC_KEY_PASSWORD`
> 以及 `APPLE_ID` / `APPLE_APP_SPECIFIC_PASSWORD` / `APPLE_TEAM_ID`，
> 再把 `package.json` 里 `mac.hardenedRuntime` 改回 `true` 并补上 entitlements。
> 在那之前，`mac.identity: "-"`（ad-hoc 签名）已经能保证包内签名自洽、
> 不会出现「签名无效/已损坏」这种连系统设置都救不回来的情况。

## 关闭应用后的进程清理

PyInstaller 的 `--onefile` 产物是**两层进程**（外层 bootloader + 内层真身），
所以清理不能只杀一层。约定：

- `main.cjs` 在 Windows 上用 `taskkill /T /F` 杀整棵进程树；
  macOS 上 PyInstaller 会把信号转发给内层，`SIGTERM` 即可。
- `server.py` 另起一个守护线程盯着 `LUCKY_DRAW_PARENT_PID`（Electron 主进程的 PID），
  主进程一消失就自杀 —— 覆盖「任务管理器结束任务 / Force Quit / 崩溃」这类
  退出钩子根本没机会跑的场景。

改动退出逻辑后，用上面那个冒烟脚本回归一次。

## 故障排查：双击了却「什么都没发生」

按下面顺序查，前两条能解释绝大多数「静默退出」：

### 1. 环境里混进了 `ELECTRON_RUN_AS_NODE`

这个变量一旦存在，Electron 会**退化成纯 Node**：

- 不带参数启动 → 它会安静地读 stdin 然后退出，**没有任何窗口、没有任何日志**；
- 带任何 `--开关`（比如 `--disable-gpu`）→ 被 Node 的选项解析器拒掉，
  输出 `bad option: --disable-gpu`。

它常常是被某些开发工具 / 自动化宿主写进全局环境变量的。排查：

```powershell
echo $env:ELECTRON_RUN_AS_NODE
```

有输出就删掉它（系统属性 → 环境变量），然后重开应用。同类问题还可能是 `NODE_OPTIONS`。

> 这也意味着：**从 Agent/脚本里启动应用调试时**，要先 `env -u ELECTRON_RUN_AS_NODE`，
> 否则会得到一堆和产品无关的假故障。`tools/packaged-smoke/verify_packaged.js` 已经
> 自动处理了这一点。

### 2. 机器没有可用的显卡（虚拟机 / 远程桌面 / 驱动异常）

表现是日志里连续出现 `GPU process exited unexpectedly` 然后
`FATAL: GPU process isn't usable. Goodbye.`。

`main.cjs` 会自救：在 FATAL 之前 Chromium 一定会先派发 `child-process-gone(type=GPU)`，
监听器攒够两次就 release 单实例锁、用 `app.relaunch()` 带 `--disable-gpu --in-process-gpu`
重启一次。前提是 **`--in-process-gpu` 必须在**（见上文说明），只有 `--disable-gpu` 救不回来。

想确认这条链是否正常，跑：

```bash
node tools/packaged-smoke/verify_packaged.js --app release/win-unpacked --mode app --no-app-args
```

看到 `GPU 自愈重启链可用` 就说明重启后的实例确实起来了。

