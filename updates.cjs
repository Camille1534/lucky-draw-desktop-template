const { app, ipcMain, dialog } = require('electron');
const http = require('http');

function registerUpdates(getWindow, getPort, updateToken) {
  if (process.platform !== 'win32') return;
  let running = false;
  const message = (detail) => dialog.showMessageBox(getWindow(), {type:'info', title:'软件更新', message:detail});
  ipcMain.handle('updates:check', async event => {
    const win = getWindow();
    if (!win || event.sender !== win.webContents ||
        new URL(event.senderFrame.url).origin !== `http://127.0.0.1:${getPort()}`) throw Error('无效的更新请求');
    if (running) return message('正在检查或下载更新，请稍候。');
    if (!app.isPackaged || process.env.PORTABLE_EXECUTABLE_DIR) {
      return message('自动更新适用于 Windows 安装版。请先安装 Setup.exe，之后即可在应用内更新。');
    }
    const { autoUpdater } = require('electron-updater');
    autoUpdater.autoDownload = false;
    autoUpdater.autoInstallOnAppQuit = false;
    autoUpdater.allowPrerelease = false;
    autoUpdater.allowDowngrade = false;
    // A rejected check/download is handled below, without an unhandled EventEmitter error.
    if (!autoUpdater.listenerCount('error')) autoUpdater.on('error', () => {});
    running = true;
    try {
      const result = await autoUpdater.checkForUpdates();
      if (!result || !result.isUpdateAvailable) return await message(`当前已是最新版本（${app.getVersion()}）。`);
      const answer = await dialog.showMessageBox(win, {type:'info', title:'发现新版本',
        message:`当前版本 ${app.getVersion()}，可更新至 ${result.updateInfo.version}`,
        detail:'下载期间可继续使用。安装需要退出应用，活动数据会保留。',
        buttons:['下载更新','稍后'], defaultId:0, cancelId:1});
      if (answer.response !== 0) return;
      const progress = p => win.setProgressBar(p.percent / 100);
      autoUpdater.on('download-progress', progress);
      try { await autoUpdater.downloadUpdate(); }
      finally { autoUpdater.removeListener('download-progress', progress); win.setProgressBar(-1); }
      const install = await dialog.showMessageBox(win, {type:'info', title:'更新已下载',
        message:'现在退出并安装更新？', detail:'如正在进行活动，可以稍后再安装。',
        buttons:['退出并安装','稍后'], defaultId:1, cancelId:1});
      if (install.response !== 0) return;
      // Atomically prevent new draws after checking all sessions for pending winners.
      await new Promise((resolve,reject) => {
        const request = http.request({hostname:'127.0.0.1', port:getPort(), path:'/api/updates/prepare',
          method:'POST', headers:{'X-Update-Token':updateToken}, timeout:4000}, response => {
          let raw=''; response.on('data', d=>raw+=d); response.on('end',()=>{
            try { const data=JSON.parse(raw); data.ok?resolve():reject(Error(data.error)); }
            catch(e) { reject(e); }
          });
        });
        request.on('error', reject); request.on('timeout',()=>request.destroy(Error('本地服务响应超时'))); request.end();
      });
      autoUpdater.quitAndInstall(false, true);
    } catch(e) {
      await message('暂时无法完成更新：' + e.message + '\n请检查网络及发布地址是否可访问，稍后可重试。');
    } finally { running = false; }
  });
}
module.exports = { registerUpdates };
