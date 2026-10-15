# 05 · 没有视觉能力时读图

## 场景

用户发来一张截图，要求翻译、转录、解释。但当前模型**不支持读图**——`Read` 工具对 PNG/JPG 直接报 `does not support reading images`。

正确的反应不是「我看不到图片」，而是：**这台 Windows 自带 OCR 引擎，五秒钟就能把文字取出来。**

---

## 可用的引擎

`Windows.Media.Ocr` 是系统内置的识别引擎：

| 特性 | 情况 |
|---|---|
| 语言 | 取决于已安装语言包，常见 `en-US`、`zh-Hans-CN` |
| 网络 | **完全离线** |
| 依赖 | 无（不需要 tesseract，不需要 pip 包） |
| 平台 | Windows 10 1809+ / Windows 11 |

先枚举可用语言：

```powershell
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }
```

实测输出：

```
en-US, zh-Hans-CN
```

中文引擎读拉丁字母同样可用，中英混排的截图可以两个引擎都跑一遍取并集。

---

## ⚠️ Windows 运行时对象在 PowerShell 里有个硬伤

**这是本文档最重要的一节，先看它，能省掉一整轮无效调试。**

在 PowerShell 5.1（实测版本 5.1.26100.9444）上，**异步操作成功返回的 WinRT 对象，一旦被脚本层访问成员，就会失败**：

```
System.ArgumentException: 对象的类型不能为 Windows 运行时类型。
参数名: obj
   在 System.Runtime.InteropServices.Marshal.GetComObjectData(Object obj, Object key)
   在 System.Management.Automation.ComInterop.ComObject.ObjectToComObject(Object rcw)
   在 CallSite.Target(Closure , CallSite , Object )
```

### 关键细节：失败发生在**结果已被拿到之后**

用一个可复现的最小例子说明。下面每一步的实测结果：

```powershell
# ① 拿异步操作 —— 成功
$op = [Windows.Storage.StorageFile]::GetFileFromPathAsync('C:\Windows\Web\Wallpaper\Windows\img0.jpg')
#    → 返回 System.__ComObject

# ② 用 AsTask 泛型方法搭桥 —— 成功
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$task = $asTaskGeneric.MakeGenericMethod([Windows.Storage.StorageFile]).Invoke($null, @($op))
#    → Task`1[Windows.Storage.StorageFile]

# ③ 等待 —— 成功！任务正常完成
$task.Wait(20000)      # → True
$task.Status           # → RanToCompletion

# ④ 读结果、访问它的成员 —— 失败
$file = $task.Result
$file.Path             # → ArgumentException: 对象的类型不能为 Windows 运行时类型
```

**异步管线完全正常，任务 `RanToCompletion`。挂掉的是 PowerShell 试图把返回的 WinRT 对象包装成 `__ComObject` 以便提供成员访问的那一刻。**

这意味着任何「取对象 → 调它的方法 → 再取对象」的 WinRT 流程都会在 PowerShell 里断掉。OCR 恰好就是这种流程（拿文件 → 开流 → 解码 → 识别），所以：

> **在本机 PowerShell 5.1 上，走 WinRT 的 OCR 流程不可行。**

### 为什么 `Add-Type` 路线没这个问题

C# 编译出来的代码是强类型的，编译器直接生成对 WinRT 接口的调用，不经过 PowerShell 那层动态包装。所以同样的流程在 C# 里完全正常。

### 出路

**如果 `Add-Type` 可用**（先用 `scripts/diagnose.ps1` 确认 `addTypeCompile`），用 C# 调 WinRT：

```powershell
$code = @'
using System;
using System.Threading.Tasks;
using Windows.Media.Ocr;
using Windows.Graphics.Imaging;
using Windows.Storage;
using Windows.Storage.Streams;

public static class OcrHelper {
    public static string Run(string path, string languageTag) {
        // WinRT 的异步在 C# 里可以直接 await，不需要手工搭桥
        return Task.Run(async () => {
            var file = await StorageFile.GetFileFromPathAsync(path);
            using (var stream = await file.OpenAsync(FileAccessMode.Read)) {
                var decoder = await BitmapDecoder.CreateAsync(stream);
                var bitmap = await decoder.GetSoftwareBitmapAsync(
                    BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied);

                var language = new Windows.Globalization.Language(languageTag);
                var engine = OcrEngine.TryCreateFromLanguage(language);
                if (engine == null) return "NO_ENGINE:" + languageTag;

                var result = await engine.RecognizeAsync(bitmap);
                var lines = new System.Text.StringBuilder();
                foreach (var line in result.Lines) {
                    lines.AppendLine(line.Text);
                }
                return lines.ToString();
            }
        }).GetAwaiter().GetResult();
    }
}
'@

Add-Type -TypeDefinition $code -Language CSharp -Reference @(
    'System.Runtime.WindowsRuntime.dll'
)
```

> **状态说明**：本节代码基于 WinRT API 的公开签名写出，但**尚未在本机完整验证**——本机的 `Add-Type` 可用（自检确认），但引用 WinRT 元数据还需要 `Windows.winmd` 或 SDK 引用，这一步依赖具体的 .NET / SDK 安装情况。**请在你的机器上试一遍再用于生产**，并把结果反馈到 issues。
>
> 相比之下，本文档的其它内容（回收站机制、`LoadFrom`、执行策略绕法、ASCII 核对技巧）都是本机实测通过的。

**如果不方便用 C#**，还有两条路：

1. **反射驱动**：所有 WinRT 调用都通过 `MethodInfo::Invoke`，返回值直接作为下一个 `Invoke` 的参数，绝不让 PowerShell 表达式引擎接触对象。理论上可行，但代码脆弱，且每次 `Invoke` 的返回值仍会经过 PowerShell 的包装层，需要实测验证。
2. **换用 TrueType/其它 OCR**：如果环境里有别的 OCR 服务，优先用它。

---

## 即使 OCR 不可用，这些技巧仍然有效

### 多尺度识别

小字常见「1 倍读不出、3 倍才对」。同一张图用 1x / 2x / 3x 各跑一遍，取最清晰的一档：

```
1x 读到：E)(tensions
3x 读到：Extensions      ← 取这个
```

**不要取第一个结果**，不同缩放的输出可能不一样。

### 旋转

**竖排文字在 0° 下完全读不出**，需要把图旋转 90° 再识别：

- `1` = 顺时针 90°
- `2` = 180°
- `3` = 逆时针 90°

版面里有竖排中文或侧边标签时，这一步是关键，不是可选项。

### 裁剪 + 放大

小字区域单独裁出来放大再识别，效果远好于整图识别：

```bash
python scripts/pngtool.py in.png --crop 120,340,480,60 --scale 4 --out region.png
```

**不要用 `BitmapTransform.Bounds` 做裁剪。** 实测它设置和读回都正常，但产出的 bitmap 交给 OCR 会返回**空白结果**——这是 WinRT 侧的一个坑。用 Python 先裁好再喂进去。

### ASCII 点阵核对版面

OCR 只给文字，不给版面。想确认「哪个标签在哪个位置」，把图转成 ASCII：

```bash
python scripts/pngtool.py in.png --ascii 170                     # --mode mean，看布局
python scripts/pngtool.py in.png --ascii 170 --mode min          # 看笔画
```

| `--mode` | 看什么 |
|---|---|
| `mean`（默认） | 色块、边框、卡片布局 |
| `min` | 文字笔画 |
| `max` | 深色底上的亮字 |

**这一步在 OCR 走不通时尤其重要**——它是唯一能让你「看见」图片的通道。

---

## 结果可信度

OCR 不是万能的，以下几类容易出错：

| 情况 | 表现 |
|---|---|
| 竖排文字、艺术字、低对比度小字 | 识别率低，需第 2 节的多手段交叉验证 |
| 数字串（版本号、金额、ID） | **至少两轮不同尺度确认** |
| 图形、图标 | 被误读成孤立的 `0`、`O`、`口` 等单字符 |

**最后一条要特别小心**：结果里出现孤立的单字符时，多半是图形被误读或是竖排文字的碎片，**不要当成真实文本写进结论**。

---

## 小结

| 情况 | 怎么做 |
|---|---|
| 需要精确表格结构，或来源是 PDF | 用专门的 PDF 文本提取工具 |
| 中英混排的截图，只需要文字 | Windows OCR + 多尺度 + 旋转 |
| **PowerShell 5.1 + WinRT** | ⚠️ 会撞对象包装的墙，改用 C# |
| OCR 完全不可用 | ASCII 点阵核对版面，人工判读 |
| 需要真正的视觉理解 | 只能换支持视觉的模型 |
