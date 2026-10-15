# 03 · 不装库处理图像

## 两条路

| 路线 | 覆盖格式 | 依赖 | 适用 |
|---|---|---|---|
| **纯标准库** | PNG（读写）、BMP / PPM / PGM | 无 | 任何环境，最保险 |
| **GDI+（`System.Drawing`）** | JPEG / GIF / TIFF / BMP / PNG，含高质量缩放 | Windows + `LoadFrom` | 需要解码 JPEG 时 |

两条路可以组合：用 GDI+ 把 JPEG 转成 PNG，之后全部交给纯 Python 处理。

---

## 路线一：纯标准库

PNG 的格式（zlib 压缩 + 逐行滤波）完全可以用标准库实现，不需要任何依赖：

| 组件 | 用什么 |
|---|---|
| 解压 | `zlib` |
| 分块解析 | `struct` |
| 滤波还原 | 纯 Python 循环 |
| 调色板量化 | 自实现中位切分（median cut） |
| 编码 | `zlib` + `struct` 拼 chunk |

完整实现见 [stdpng](https://github.com/ErLing78/-PNG-pngtool-)，单文件版本见 [`../scripts/pngtool.py`](../scripts/pngtool.py)。

### 性能预期（1024×1024，普通笔记本）

| 操作 | 纯 Python |
|---|---|
| 解码 | ~1.5 s |
| 最近邻缩放 | ~0.3 s |
| 量化到 16 色 | ~2 s |

适用于精灵图、图标、Tile、头像。**不适合批量处理照片**——那种场景下如果 Pillow 装得上，就用 Pillow。

### 关键优化

- **像素用扁平 `bytearray`，不要用嵌套 list-of-tuples**。这是最大的一处差异，通常快一个数量级。
- **量化时按唯一 RGB 三元组缓存最近色查找**。一张 256 色的图只有几百种不同的颜色，缓存命中率极高。
- 缩放时**预计算列映射数组**，不要在每像素内做除法。

### 压缩到体积上限

真彩色 PNG 很容易超限（512×512 可能 400~900 KB）。做法是转成 8 位索引 PNG + 中位切分调色板：

```
1024×1024 真彩色 PNG  →  512×512 / 256 色索引 PNG
     ~1 MB                    ~90 KB
```

卡通、插画、UI 类图像用 256 色几乎无损；写实照片会有轻微色带，降到 128 色更明显。**不要为了压体积无脑降色数**，先把尺寸降到要求值，色数不够再降。

---

## 路线二：GDI+（解码 JPEG 等）

`System.Drawing` 走的是系统自带的 GDI+ 编解码器。用 `LoadFrom` 加载，不需要 `Add-Type`：

```powershell
$frameworkRoot = if ([Environment]::Is64BitProcess) {
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
} else {
    'C:\Windows\Microsoft.NET\Framework\v4.0.30319'
}
$null = [System.Reflection.Assembly]::LoadFrom((Join-Path $frameworkRoot 'System.Drawing.dll'))

$image = [System.Drawing.Image]::FromFile('C:\in.jpg')
"$($image.Width)x$($image.Height)"
$image.Save('C:\out.png', [System.Drawing.Imaging.ImageFormat]::Png)
$image.Dispose()
```

完整版（含批量、等比缩放、JSON 报告）见 [stdpng 的 image_transcode.ps1](https://github.com/ErLing78/-PNG-pngtool-/blob/main/tools/image_transcode.ps1)。

### 实测

| 操作 | 结果 |
|---|---|
| 3840×2400 JPEG → PNG | 成功，6.3 MB |
| `-MaxSize 256` | 256×160（等比） |
| 单目录 6 个文件批量 | 一次调用完成 |
| 输入文件不存在 | 抛异常，不静默成功 |

### 两个必须注意的点

**1. `FromFile` 会锁住文件**

`Image.FromFile` 返回的对象持有文件句柄，直到 `Dispose()`。如果之后还要移动或删除原文件，会失败。

```powershell
$source = [System.Drawing.Image]::FromFile($path)
$detached = New-Object System.Drawing.Bitmap($source)   # 拷贝像素
$source.Dispose()                                        # 这时才释放锁
# 之后操作 $detached
```

**2. 缩放要用高质量插值**

默认插值是 `Bilinear` 或更低。缩小时不设高质量会出摩尔纹：

```powershell
$graphics = [System.Drawing.Graphics]::FromImage($target)
$graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$graphics.DrawImage($source, 0, 0, $width, $height)
```

**反过来**：如果是**像素画**，任何插值都是灾难。那种情况用路线一的最近邻缩放，不要走 GDI+ 的插值。

---

## 为什么不要自己写 JPEG 解码器

baseline JPEG 要用到霍夫曼解码、反量化、IDCT、色度上采样、色彩空间转换，几百行正确的代码起步。

而**写错的 JPEG 解码器不会崩，它会把人的照片解出细微错误的颜色**——这种 bug 极难发现，等发现时已经用了几百张图。

所以：**要么用系统编解码器（GDI+），要么明确拒绝并给出替代方案。** 不要交付一个「差不多能用」的实现。

---

## 无视觉能力时如何核对图像

当模型或工具无法显示图片时，把图渲染成 ASCII 字符画，用文字读出来：

```bash
python scripts/pngtool.py in.png --ascii 120              # 默认 --mode mean
python scripts/pngtool.py in.png --ascii 120 --mode min    # 读笔画
```

`--mode` 决定每个字符格子代表什么：

| 取值 | 含义 | 用途 |
|---|---|---|
| `mean` | 格子内亮度**均值**（默认） | 看色块、边框、整体布局 |
| `min` | 格子内**最暗**值 | 看细笔画和文字 |
| `max` | 格子内**最亮**值 | 看深色底上的亮字 |

把区域裁小再放大，字符密度表 ` .:-=+*#%@` 由浅到深，字就清晰可读了。

**这是处理图像流水线时的必需品**，不是玩具：改完一个缩放算法，你需要一个不依赖图形界面的方式确认输出没有崩坏。

---

## 小结

| 需求 | 走哪条路 |
|---|---|
| 精灵图缩放、调色板量化、裁剪旋转 | 纯标准库（[stdpng](https://github.com/ErLing78/-PNG-pngtool-)） |
| 解码 JPEG / TIFF / GIF | GDI+ via `LoadFrom`，先转 PNG 再处理 |
| 把图压到体积上限 | 中位切分量化 + 8 位索引 PNG |
| 像素画缩放 | **最近邻**，绝不用插值 |
| 照片缩放 | GDI+ `HighQualityBicubic` |
| 无图形界面下核对结果 | ASCII 预览 |
