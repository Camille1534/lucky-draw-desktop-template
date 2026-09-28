const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const readJson = file => JSON.parse(fs.readFileSync(path.join(root, file), 'utf8'));
const writeJson = (file, value) => fs.writeFileSync(path.join(root, file), `${JSON.stringify(value, null, 2)}\n`);

const config = readJson('brand.config.json');
const required = ['packageName', 'appName', 'appId', 'version', 'eventName', 'eventMeta', 'screenTagline'];
for (const key of required) {
  if (!String(config[key] || '').trim()) throw new Error(`brand.config.json 缺少 ${key}`);
}
if (!/^\d+\.\d+\.\d+$/.test(config.version)) {
  throw new Error('version 必须是三段式，例如 1.0.0 或 1.0.1');
}
if (!/^[a-z0-9][a-z0-9._-]*$/.test(config.packageName)) {
  throw new Error('packageName 只能使用小写字母、数字、点、下划线或连字符');
}

const packageJson = readJson('package.json');
packageJson.name = config.packageName;
packageJson.version = config.version;
packageJson.description = `${config.appName} desktop lucky draw application`;
packageJson.build.appId = config.appId;
packageJson.build.productName = config.appName;
packageJson.build.nsis.shortcutName = config.appName;
writeJson('package.json', packageJson);

const lock = readJson('package-lock.json');
lock.name = config.packageName;
lock.version = config.version;
if (lock.packages && lock.packages['']) {
  lock.packages[''].name = config.packageName;
  lock.packages[''].version = config.version;
}
writeJson('package-lock.json', lock);

const clientConfig = {
  appName: config.appName,
  eventName: config.eventName,
  eventMeta: config.eventMeta,
  screenTagline: config.screenTagline,
  theme: config.theme || {},
  assets: config.assets || {},
};
const output = `// Generated from brand.config.json by npm run brand:apply.\nwindow.BRAND_CONFIG = ${JSON.stringify(clientConfig, null, 2)};\n`;
fs.writeFileSync(path.join(root, 'dist', 'brand.config.js'), output);

console.log(`已应用品牌配置：${config.appName} v${config.version}`);
