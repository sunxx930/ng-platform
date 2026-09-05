# NG-AI-Platform Windows 打包 + 压缩分发流程（单文件版）

> Windows 版 = **onefile 单文件 exe**，分发最简。
> 打包必须在 **Windows 本机**执行（PyInstaller 不跨平台）。Windows 版不做代码签名（无该要求）。

## 三步

1. **打包**：把项目拷到 Windows，双击 `scripts/build_windows.bat`
   → 自动装依赖 + 跑 `pyinstaller ng-platform.spec --noconfirm`
   → 产物就一个：**`dist\NG-AI-Platform.exe`**

   > 只在你改了**前端源码**时，先跑 `cd frontend && npm install && npm run build` 刷新 dist 再打包。

2. **验证**：双击 `dist\NG-AI-Platform.exe` → 浏览器自动开 `http://127.0.0.1:8001`。

3. **压缩/分发**（可选）：
   - 右键 `NG-AI-Platform.exe` → 发送到 → 压缩(zipped)文件夹；或 PowerShell `Compress-Archive -Path dist\NG-AI-Platform.exe -DestinationPath dist\NG-AI-Platform-win.zip`
   - 直接传 exe 也行，看下载平台是否允许 exe
   - 上传官网/网盘；用户解压后双击 exe 即可

## 用户侧
- 解压 → 双击 `NG-AI-Platform.exe`，无命令行门槛
- Windows 弹 SmartScreen →「更多信息 → 仍要运行」（未签名，正常）
- 个别杀软误报 → 加白名单

## 注意
- exe 图标：`app/static/icon.ico` 存在即自动用（现已是正式 logo 白底青鲸版）
- **无黑窗口**（spec Windows 分支 `console=False`，已改）；调试要打印时临时改回 `True` 重打即可
- onefile 特性：单文件启动时会自解压到临时目录（首启稍慢 1–2 秒），换取分发简单；杀软对单文件偶有更敏感，误报加白名单即可
