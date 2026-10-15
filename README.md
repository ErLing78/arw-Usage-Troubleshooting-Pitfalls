# awesome-restricted-windows

**在被安全策略锁死的 Windows 机器上，还能把活干完的那些技法。**

`Add-Type` 被拦、执行策略是 `Restricted`、`pip` 连不上网、PATH 里没有 ImageMagick、没有 `trash` 命令——这些在正常开发机上一辈子遇不到，在企业管控机、内网隔离机、客户现场机器上却是默认状态。

网上讲这些的资料基本没有，因为**能写文档的人不用这种机器，用这种机器的人没空写文档**。

这里记录的都是在一台真实受控机器上实测过的结论，包含失败的尝试——那些往往比成功更有用。

---

## 这个项目解决什么问题

在你的开发机上，处理一张图片、删一个目录、读一张截图里的文字，都是一行命令的事。到了受控机器上，这一行命令会以各种方式失败：

| 你以为能用的 | 在受控机器上 |
|---|---|
| `pip install pillow` | `No matching distribution found`（离线）或 403（镜像源按 UA 拦截） |
| `magick` / `convert` / `ffmpeg` | PATH 里没有（而且 Windows 的 `convert.exe` **是磁盘格式转换工具，不是 ImageMagick**） |
| `Add-Type -AssemblyName System.Drawing` | `Command blocked for security` |
| 直接跑 `.\script.ps1` | 无输出、exit 1、无报错——因为执行策略是 `Restricted` |
| `gio trash` / `trash` | Windows 根本没这个命令 |
| 把图丢给模型看 | 模型可能没有视觉能力，`Read` 直接拒绝图片 |
| PowerShell 跑完看输出 | 某些宿主**不返回 stdout** |

**这个项目做两件事**：一是给一个自检脚本，让你五分钟内摸清这台机器什么能用；二是针对每一条被堵死的路，记录一条实测可用的绕法。

---

## 主要功能

### 1. 环境自检（双脚本）

| 脚本 | 覆盖范围 |
|---|---|
| `scripts/diagnose.ps1` | 程序集加载、`Add-Type`、执行策略、外部程序、WinRT、回收站、GDI+ 可用性 |
| `scripts/diagnose.py` | 标准库完整性、第三方库、pip 镜像源可达性、Win32 ctypes、回收站 |

两者都输出 JSON 报告，并在末尾给出一段把原始探测翻译成结论的 `capabilities` 字段：

```
addTypeCompile        yes     能否即时编译 C#
loadFromAssembly      yes     能否按路径加载框架程序集
gdiPlusImaging        yes     能否用 GDI+ 解码 JPEG / 编码 PNG
screenCapture         yes     能否截屏
offlineOcr            yes     能否离线 OCR
recycleBinAccess      yes     能否访问回收站目录
```

### 2. 零依赖工具脚本

| 脚本 | 能力 | 依赖 |
|---|---|---|
| `scripts/sh_delete.py` | 回收站 / 永久删除、解析并清理回收站条目、按原路径前缀精确筛选 | 仅标准库 |
| `scripts/pngtool.py` | PNG 裁剪、旋转、最近邻缩放、ASCII 预览、元信息 | 仅标准库 |
| `scripts/diagnose.py` | 环境自检 | 仅标准库 |
| `scripts/diagnose.ps1` | 环境自检 | 无 |

**全部只用标准库，全部可以单独拷走使用**，不依赖同仓库的其它文件。

### 3. 踩坑文档

`docs/01–05`，覆盖环境判定、程序集加载与执行策略、图像处理、删除与回收站、无视觉读图。

---

## 安装方法

**没有依赖，不需要安装。** 这是本项目的核心约束——如果需要依赖，就没资格出现在这里。

### 方式一：克隆

```bash
git clone https://github.com/ErLing78/awesome-restricted-windows-.git
cd awesome-restricted-windows
```

### 方式二：只拿你要的那一个脚本

每个脚本都是自包含的。在受控机器上没法 clone 时，直接拷单个文件即可：

```bash
# 传到目标机器后
python sh_delete.py --help
python pngtool.py --help
python diagnose.py --help
```

### 环境要求

| 脚本 | 要求 |
|---|---|
| `diagnose.ps1` | Windows + PowerShell 5.1 或更高 |
| `sh_delete.py` | Windows + Python 3.8+（用到 `ctypes` 调 Win32） |
| `diagnose.py` | Python 3.8+（非 Windows 上也能跑，会如实标注哪些项不适用） |
| `pngtool.py` | Python 3.8+，跨平台 |

文档部分无任何要求，直接读。

**实测版本**：Python **3.9.0** 与 **3.13.14** 上全部验证通过（`tests/readme_examples_test.py`）。CI 覆盖 3.8 / 3.11 / 3.13。

> Python 版本差异不是小事：`sh_delete.py` 里有一处代码在 3.9 上会抛 `ValueError: embedded null character`，在 3.13 上却静默通过。详见 [docs/01](docs/01-environment.md)。

---

## 使用方法

### 第一步：先跑自检

不要猜。在受控机器上最浪费时间的做法是试错——试一次五分钟，试错十次一小时。

```powershell
# PowerShell 侧
& ([scriptblock]::Create((Get-Content -Raw .\scripts\diagnose.ps1)))
# 报告写入 diagnose_powershell.json
```

```bash
# Python 侧
python scripts/diagnose.py
# 报告写入 diagnose_python.json
```

> **为什么 `.ps1` 要这么绕着执行？** 因为执行策略是 `Restricted` 时，直接跑 `.ps1` 会**静默失败**——无输出、无报错、exit 1。把文件内容读成文本再执行可以绕过，且不改动系统策略。详见 [docs/02](docs/02-loading-assemblies.md)。

### 第二步：按需查文档

| 你要做的事 | 看哪篇 |
|---|---|
| 搞清楚这台机器什么能用 | [docs/01 环境判定](docs/01-environment.md) |
| 加载程序集、跑 `.ps1` | [docs/02 程序集与执行策略](docs/02-loading-assemblies.md) |
| 不装库处理图片 | [docs/03 图像处理](docs/03-image-processing.md) |
| 删文件、清回收站 | [docs/04 删除与回收站](docs/04-delete-and-recycle-bin.md) |
| 模型读不了图时读图 | [docs/05 无视觉读图](docs/05-ocr-without-vision.md) |

### 第三步：用脚本

所有涉及删除的操作**默认是 dry run**，必须显式加 `--execute` 才会真动手。这是刻意的设计。

---

## 使用示例

### 示例 1：自检这台机器

```powershell
& ([scriptblock]::Create((Get-Content -Raw .\scripts\diagnose.ps1))) -OutputPath C:\report.json
```

实测输出尾部：

```
diagnostic written to C:\report.json
0 probe(s) failed
```

### 示例 2：把 JPEG 转成 PNG（不用装任何库）

需要 [stdpng 的 `tools/image_transcode.ps1`](https://github.com/ErLing78/-PNG-pngtool-/blob/main/tools/image_transcode.ps1)（走 `System.Drawing`，不需要 `Add-Type`）：

```powershell
# 先把脚本读进 scriptblock（绕开 Restricted 策略，见 docs/02）
$sb = [scriptblock]::Create((Get-Content -Raw .\image_transcode.ps1))

# 单个文件
& $sb -InputPath 'C:\in.jpg' -OutputPath 'C:\out.png'

# 批量，最长边缩到 1024
& $sb -InputPath 'C:\shots' -OutputPath 'C:\work\png' -MaxSize 1024
```

实测：3840×2400 的 JPEG 解码并写成 PNG 正常；`-MaxSize 256` 得到 256×160；目录批量一次调用完成；输入文件不存在时抛异常而不是静默成功。

### 示例 3：裁剪、放大、预览一张 PNG

```bash
# 看元信息
python scripts/pngtool.py in.png --info

# 裁出小字区域并放大 4 倍（交给 OCR 前常用）
python scripts/pngtool.py in.png --crop 120,340,480,60 --scale 4 --out region.png

# 旋转竖排文字（90 = 顺时针）
python scripts/pngtool.py in.png --rotate 90 --out upright.png

# 无图形界面时把图变成字符画来读
python scripts/pngtool.py in.png --ascii 120                 # --mode mean，看布局
python scripts/pngtool.py in.png --ascii 120 --mode min      # 看文字笔画
```

### 示例 4：删除目录并真正回收空间

```bash
# ① 先看会删什么（默认 dry run，不会动任何东西）
python scripts/sh_delete.py delete "C:\build\cache" --mode permanent

# ② 确认后执行
python scripts/sh_delete.py delete "C:\build\cache" --mode permanent --execute

# ③ 关键一步：目录删除会被环境转存回收站，查一下
python scripts/sh_delete.py scan --prefix "C:\build\cache"

# ④ 确认这些条目确实是本次任务产生的，再清掉
python scripts/sh_delete.py purge --prefix "C:\build\cache" --execute
```

实测第 ③ 步的输出（删一个含 3 个文件的目录后）：

```
6 matching entries, 0.2 MB total
  A1B2C3  <同一时刻>  0.00 MB
      ...\sandbox\sd_test
  D4E5F6  <同一时刻>  0.00 MB
      ...\sandbox\sd_test\sub\b.txt
  ...
```

**6 条条目对应目录树的每一个节点。** 只做第 ② 步，空间一点没释放——只是从「占着磁盘」变成「占着回收站」。这就是为什么要加第 ③④ 步。

详见 [docs/04](docs/04-delete-and-recycle-bin.md)。

### 示例 5：批量删除（用清单文件）

```
# cleanup.txt
# 每行一个路径，# 开头是注释
C:\build\cache
C:\build\tmp
```

```bash
python scripts/sh_delete.py delete --manifest cleanup.txt --mode recycle --execute
```

### 示例 6：在 Python 里直接调

```python
import sys
sys.path.insert(0, "scripts")
from sh_delete import sh_delete, scan_recycle_bin

# 移入回收站（可恢复）
sh_delete([r"C:\build\cache"], to_recycle=True)

# 永久删除
sh_delete([r"C:\build\cache"], to_recycle=False)
```

---

## 症状速查

| 你遇到的 | 真正的原因 | 去哪看 |
|---|---|---|
| `pip install X` 报 403 或无网络 | 镜像源判定 UA 异常 / 完全离线 | [docs/01](docs/01-environment.md) |
| `Add-Type` 报 *Command blocked for security* | 安全策略拦截即时编译 | [docs/02](docs/02-loading-assemblies.md) |
| `.ps1` 跑起来没反应、exit 1 | 执行策略是 `Restricted` | [docs/02](docs/02-loading-assemblies.md) |
| PowerShell 跑完什么都不回显 | 宿主不返回 stdout | [docs/01](docs/01-environment.md) |
| 没有 `ImageMagick` / `convert` / `ffmpeg` | 没装，也装不上 | [docs/03](docs/03-image-processing.md) |
| 没有 `trash` / `gio trash` | Windows 不提供这个命令行 | [docs/04](docs/04-delete-and-recycle-bin.md) |
| 删了大目录，空间没释放 | 删除被环境自动转存回收站了 | [docs/04](docs/04-delete-and-recycle-bin.md) |
| 按前缀扫回收站扫不到条目 | **路径里有 8.3 短名**（`LONGPATH~1.USR`），而元数据存的是长名 | [docs/04](docs/04-delete-and-recycle-bin.md) |
| Python 3.9 下调 Win32 API 报 `embedded null character` | `c_wchar_p` 字段不接受含 `\0` 的字符串（3.12+ 才放宽） | [docs/01](docs/01-environment.md) |
| 模型读不了图，但用户发了图 | 模型无视觉能力 | [docs/05](docs/05-ocr-without-vision.md) |
| scriptblock 里变量值莫名其妙不对 | 被调用者的参数名遮蔽了 | [docs/02](docs/02-loading-assemblies.md) |
| ctypes 调 Win32 莫名其妙失败 | 默认 `restype` 截断了 64 位句柄 | [docs/01](docs/01-environment.md) |

---

## 技法清单（摘要）

完整内容在 `docs/` 里，这里是索引。

### 一、加载程序集，绕开 `Add-Type`

```powershell
# 被拦
Add-Type -AssemblyName System.Drawing

# 可用
$null = [System.Reflection.Assembly]::LoadFrom(
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Drawing.dll'
)
```

→ [docs/02](docs/02-loading-assemblies.md)

### 二、执行 `.ps1`，绕开 `Restricted` 策略

```powershell
& ([scriptblock]::Create((Get-Content -Raw 'C:\path\to\script.ps1')))
```

→ [docs/02](docs/02-loading-assemblies.md)

### 三、图像处理，不装任何库

纯标准库解析 PNG 并做缩放 / 量化 / 裁剪；JPEG 等格式走 `System.Drawing`。

→ [docs/03](docs/03-image-processing.md)，完整库见 [stdpng](https://github.com/ErLing78/-PNG-pngtool-)

### 四、删除与回收站

`ctypes` 直调 `shell32.SHFileOperationW`。注意**目录删除会被转存回收站**，需要两步清理。

→ [docs/04](docs/04-delete-and-recycle-bin.md)

### 五、模型读不了图时读图

Windows 自带 `Windows.Media.Ocr`，离线、多语言、零依赖。注意 PowerShell 侧有个 WinRT 对象包装的硬伤。

→ [docs/05](docs/05-ocr-without-vision.md)

---

## 测试

```bash
python tests/readme_examples_test.py   # 文档与代码是否一致
python tests/test_privacy.py           # 有没有不该公开的东西
```

### `readme_examples_test.py`

**执行本 README 和文档里出现的每一条命令**，并核对结果：脚本是否存在、是否只用标准库、`pngtool.py` 的四种调用方式、`sh_delete.py` 的 dry run 保护与完整删除-扫描-清理闭环、`diagnose.py` 输出的 `capabilities` 字段是否与文档一致。

它还会断言**文档与代码不打架**——比如文档里写的 `--mean` 参数如果 CLI 里不存在，测试就会失败。

其中包含一条针对 8.3 短名的回归检查：删除一个位于 `tempfile.gettempdir()` 下的目录，然后确认用短名前缀也能扫到条目。这条检查存在的唯一理由是——**这个 bug 真的发生过**，而且它让清理流程静默失效。

### `test_privacy.py`

**这个仓库的内容全部来自真实机器的实测，而文档会引用真实命令输出——这正是一条很容易泄漏的路径。**

第一次跑这个检查时，它在文档里找出了：账号名、主机名、另一个项目的工具目录名，以及 6 条真实的回收站条目（含条目 ID 与精确时间戳）。

现在它盯着四类东西：

| 类别 | 例子 |
|---|---|
| 凭据 | 密码/密钥赋值、私钥块、GitHub token、AWS key、Slack token、Bearer 头 |
| 身份 | Windows 账号 SID、邮箱、8.3 短用户名、主机名形态 |
| 路径 | 用户目录路径（系统路径如 `C:\Windows\...` 属正常内容，不算） |
| 引用输出 | 真实的时间戳——示意列表统一用 `<同一时刻>` 与合成 ID |

外加两条文件级检查：

- **未填的占位符**（作者名、`github.com/` 后接占位用户名的链接）。这一条**不接受豁免**，发布前必须填完。
- **构建产物与编辑器残留**：`__pycache__`、`build/`、`*.egg-info`、`*.pyc`、`.DS_Store`、`*.bak`、`*.orig` 等。

**它是保守的**：宁可误报也不放过。有正当内容被拦下时，请往 `ALLOWED_LINES` 里加豁免并写明理由，**不要放宽正则**。

如果你要往文档里贴真实命令输出，先跑一遍这个检查——**把敏感字段替换掉不该由你事后回想哪些删过、哪些没删**。

CI 在 Windows 上跑这套验证。

---

## 一条原则

**先判断能力，再动手。**

上面每一条技法都是「某条路被堵住之后找到的替代路径」，而判断哪条路被堵住，靠的是五分钟的自检，不是半小时的试错。

而且——**能力是动态的**。本机实测中，`Add-Type` 曾记录为「被策略拦截」，几个月后复测却完全可用。策略组会推送新规则，镜像源会改判定逻辑。

所以把 `scripts/diagnose.ps1` 当成本仓库的第一入口，而不是最后一招。

---

## 贡献

欢迎补充实测结论。要求见 [CONTRIBUTING.md](CONTRIBUTING.md)，核心是三条：

- 说明**具体的 Windows 版本 / PowerShell 版本 / 报错原文**
- 区分「我试过，可行」和「听说可行」
- 失败的尝试同样欢迎，注明失败现象

**特别欢迎推翻现有结论的 PR**——那比补充新技巧更有价值。

---

## License

[MIT](LICENSE)
