# 01 · 判定环境与通用陷阱

## 先判断，再动手

在受控机器上最浪费时间的做法是**试错**：猜某个库能不能装、猜某个 API 能不能调，试一次五分钟，试错十次一小时。

**先跑自检，五分钟拿到全部答案。**

```powershell
# PowerShell 侧：程序集加载、执行策略、外部程序、WinRT
& ([scriptblock]::Create((Get-Content -Raw .\scripts\diagnose.ps1)))
```

```bash
# Python 侧：标准库、第三方库、镜像源可达性、Win32 API
python scripts/diagnose.py
```

两边都会输出 JSON 报告，末尾还带一段把原始探测翻译成人话的 `capabilities`：

```
  addTypeCompile        yes     能否即时编译 C#
  loadFromAssembly      yes     能否按路径加载框架程序集
  gdiPlusImaging        yes     能否用 GDI+ 解码 JPEG / 编码 PNG
  screenCapture         yes     能否截屏
  offlineOcr            yes     能否离线 OCR
  recycleBinAccess      yes     能否访问回收站目录
```

---

## 网络与 pip 源

`pip install` 失败的两种原因要分清，因为对策完全不同。

### 原因一：完全不可达

真正的离线环境。此时唯一的路是**只用标准库**——这也是本仓库所有脚本的默认姿态。

### 原因二：源可达，但拒绝请求

实测（Windows 11，2026-10-15）：

| 源 | 结果 | 耗时 |
|---|---|---|
| `https://pypi.org/simple/` | **握手超时** | 4.3 s |
| `https://mirrors.aliyun.com/pypi/simple/` | 200 | **0.12 s** |
| `https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple/` | 200 | 0.49 s |

顺手测得的一个现象：**某些镜像源会按 User-Agent 判定客户端**。已知有源对 pip 新版的完整 UA 返回 `403`，把 UA 简化成 `pip/1.0` 就正常返回 `200`。所以「`403`」未必是网络问题，也未必是源挂了。

### 换源

```bash
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

配置落在 `%APPDATA%\pip\pip.ini`：

```ini
[global]
index-url = https://mirrors.aliyun.com/pypi/simple/
```

自检脚本会把这个文件的内容一起报出来——排查「为什么这台机器的 pip 行为和别人不一样」时，第一眼看的就是它。

### 离线安装

换源也救不了的时候，`pip install` 仍然可以用本地 wheel：

```bash
pip install --no-index --find-links=.\wheels somepackage
```

前提是你能从别的机器上把 wheel 拷进来。

---

## ⚠️ ctypes 的 `restype` 陷阱

用 `ctypes` 调 Win32 API 时，**默认的返回类型是 `c_int`（32 位有符号）**。

在 64 位系统上，这会把所有**指针和句柄截断成 32 位**。而截断通常不报错——它只是让后续调用莫名其妙地失败，或者更糟：返回一个看起来合理的错误码。

### 具体案例

拿当前用户的 SID（回收站目录以它命名）：

```python
import ctypes
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

token = ctypes.c_void_p()
# GetCurrentProcess() 返回 HANDLE（64 位指针），但 ctypes 按 c_int 取
if advapi32.OpenProcessToken(
    kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
):
    ...
```

在 64 位进程里，`GetCurrentProcess()` 的返回值高 32 位被丢掉，`OpenProcessToken` 拿到一个无效句柄，**直接返回失败**。整个函数静默走到 `return None`。

### 对策：显式声明每一个签名

```python
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.GetCurrentProcess.argtypes = []
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

advapi32.OpenProcessToken.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
]
advapi32.OpenProcessToken.restype = ctypes.c_int

advapi32.GetTokenInformation.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)
]
advapi32.GetTokenInformation.restype = ctypes.c_int

advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
]
```

**规则**：任何返回 `HANDLE` / `HWND` / `PSID` / 指针的 Win32 函数，都必须显式设 `restype`。`argtypes` 也顺手设上——类型不匹配时它会给出清晰的报错，而不是让错误在几层调用之外才浮现。

见 [`../scripts/diagnose.py`](../scripts/diagnose.py) 的 `probe_recycle_bin()`，修好之后 SID 才正确解析出来。

---

## ⚠️ 坑二：含 `\0` 的字符串赋给 `c_wchar_p`，Python 3.9 会炸

许多 Win32 API 要「双 null 结尾的字符串列表」——多个路径用单个 `\0` 分隔，末尾再加两个 `\0`。最直观的写法是直接拼字符串：

```python
op = SHFILEOPSTRUCTW()
op.pFrom = "\0".join(paths) + "\0"     # ← 在 Python 3.9 及更早版本会抛异常
```

实测结果（同一份代码，只换解释器）：

| 解释器 | 行为 |
|---|---|
| Python 3.9.0 | **`ValueError: embedded null character`** |
| Python 3.13.14 | 静默接受 |

也就是说，**这行代码在作者的机器上跑得好好的，到旧解释器上直接崩**。而这类崩溃往往发生在「删除」这种不可逆操作上，排查成本很高。

### 对策：用真正的缓冲区

```python
import ctypes
from ctypes import wintypes

buffer = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")
op = SHFILEOPSTRUCTW()
op.pFrom = ctypes.cast(buffer, wintypes.LPCWSTR)
```

两个要点：

1. **`buffer` 必须保持被引用**，直到 API 调用返回为止。赋值给局部变量即可，**不要写成内联表达式**——那样缓冲区可能在调用前就被垃圾回收，导致 API 读到已释放的内存。
2. 所有版本行为一致，实测 3.9 与 3.13 都正常。

见 [`../scripts/sh_delete.py`](../scripts/sh_delete.py) 的 `sh_delete()`。

---

## 其它已确认的平台事实

以下都是在一台 Windows 11（10.0.26100）、PowerShell 5.1、Python 3.9 / 3.13 的机器上实测的：

| 事实 | 影响 |
|---|---|
| PATH 里的 `convert` 是 `C:\Windows\System32\convert.exe` | **它是 FAT→NTFS 转换工具，不是 ImageMagick。** 探测 `convert` 存在就以为能转图，会得到一个问你要不要格式化分区的程序 |
| `ls` 对含上万条目的 `$Recycle.Bin` 会卡死 | 用 Python `os.listdir`，同目录 0.01 秒 |
| Git Bash 里 `mv` 后立刻 `ls` 偶尔读不到新文件 | 缓存延迟。关键文件再列一次，别凭一次输出下结论 |
| Windows 自带 OCR 语言包 | 常见为 `en-US`、`zh-Hans-CN`。`OcrEngine.AvailableRecognizerLanguages` 可枚举 |
| `MaxImageDimension` = 10000 | OCR 前可以放心放大到 3~4 倍 |
| 回收站目录结构 | `C:\$Recycle.Bin\<SID>\`，条目成对 `$I`/`$R`，详见 [04](04-delete-and-recycle-bin.md) |

---

## 一条元规则

上面每一条都可能是**特定于某台机器、某个时间点**的。

`Add-Type` 在本机曾经被拦、现在可用（见 [02](02-loading-assemblies.md)）。镜像源的策略会变。策略组会推送新规则。

**所以：把自检脚本当成本仓库的第一入口，而不是最后一招。**
