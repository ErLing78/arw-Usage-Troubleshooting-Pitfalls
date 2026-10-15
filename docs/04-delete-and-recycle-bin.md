# 04 · 删除与回收站

## 为什么难

Linux 和 macOS 都有一行命令能把文件丢进回收站（`gio trash`、`trash`）。Windows 没有。

而且在这类机器上，通常的替代方案也都被堵死：

| 方式 | 状态 |
|---|---|
| `gio` / `trash-put` / `recycle` | 不存在 |
| PowerShell `Add-Type` + `Microsoft.VisualBasic.FileIO` | 视策略，可能被拦 |
| Git Bash 的 `rm` | 可用，但**永久删除**，不进回收站 |
| **Python `ctypes` + `shell32.SHFileOperationW`** | **可用** |

所以主力方案是最后一条。`ctypes` 是标准库，不依赖 `Add-Type`，不受执行策略影响。

---

## 核心代码

```python
import ctypes
from ctypes import wintypes

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),   # 多路径：\0 分隔 + 双 \0 结尾
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 有 → 进回收站；无 → 永久删除
FOF_NOERRORUI = 0x0400

def sh_delete(paths, to_recycle=False):
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = "\0".join(paths) + "\0"   # ctypes 自动再补一个 \0 → 双 null 尾
    op.pTo = None
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
    if to_recycle:
        op.fFlags |= FOF_ALLOWUNDO
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res
```

**路径要求**：必须是绝对路径，且用反斜杠。原生 Windows API 不认 MSYS 的 `/c/...` 形式。

完整可用版本（含清单、批量、扫描、清理）见 [`../scripts/sh_delete.py`](../scripts/sh_delete.py)。

---

## 关于 `fFlags` 是 16 位

`SHFILEOPSTRUCTW` 里的 `fFlags` 是 **`FILEOP_FLAGS`，即 `WORD`（16 位无符号）**，不是 32 位。

写成 `ctypes.c_uint32` 看起来无害——前面几个字段刚好对齐，程序也能跑——但结构体总大小会变，`SHFileOperationW` 读到的 `fFlags` 就可能是垃圾值，表现为「有时候进回收站有时候不进」。

**如果你发现 `FOF_ALLOWUNDO` 时灵时不灵，先检查这一项。**

---

## ⚠️ 关键机制：删目录会被转存回收站

这是本仓库最值得记住的一条，因为它**违反直觉且不报错**。

### 实测记录

在一台 Windows 11（10.0.26100）、PowerShell 5.1 的机器上：

1. 建立目录树：`sd_test/` 下有 `a.txt`、`sub/b.txt`、`sub/deeper/big.bin`
2. 调用 `sh_delete(["...\sd_test"], to_recycle=False)` —— **明确没有设 `FOF_ALLOWUNDO`**，期望永久删除
3. 原位置消失 ✓
4. 但扫描回收站，出现 **6 条条目**，时间戳全部相同：

```
A1B2C3  <同一时刻>  ...\sandbox\sd_test
D4E5F6  <同一时刻>  ...\sandbox\sd_test\sub\b.txt
G7H8I9  <同一时刻>  ...\sandbox\sd_test\sub\deeper
J1K2L3  <同一时刻>  ...\sandbox\sd_test\sub\deeper\big.bin
M4N5O6  <同一时刻>  ...\sandbox\sd_test\sub
P7Q8R9  <同一时刻>  ...\sandbox\sd_test\a.txt
```

> 条目 ID 与时间已替换为示意值。要紧的是两件事：**条数是 6**（而不是 1），以及**时间戳完全相同**——这正说明它们是同一次删除操作在一瞬间展开的。

**6 条 = 目录树的每一个节点**：根目录、两个中间目录、三个文件。整棵树被逐项展开了。

### 结论

| 操作 | 实际行为 |
|---|---|
| 删除**单个文件**，无 `FOF_ALLOWUNDO` | 真删除，回收站不留条目 ✓ |
| 删除**目录**，无 `FOF_ALLOWUNDO` | **仍被转存回收站**，逐项条目化 ⚠️ |
| 删除**已在回收站内的条目** | 正确物理清除 ✓ |

第二条是本机环境的安全机制所致（推测是文件系统过滤驱动或策略层的介入），不是 `SHFileOperationW` 本身的行为。**但在这样的机器上它就是事实，必须按它设计流程。**

### 后果

「彻底删除一个 616 MB 的 `node_modules` 并释放空间」是**两步**：

1. 原位置删除（`SHFileOperationW`）
2. **解析回收站条目 → 按原路径筛选 → 删除条目**

只做第一步，空间一点没释放，只是从「占着磁盘」变成「占着回收站」。而且因为原目录已经不在了，事后很难看出问题出在哪。

---

## 回收站条目结构

位置：`C:\$Recycle.Bin\<用户SID>\`

条目成对出现：

- `$I<6位ID>[.ext]` —— 元数据（原路径、原大小、删除时间）
- `$R<6位ID>[.ext]` —— 实体内容（文件或目录）

### `$I` 元数据格式（version 2）

| 偏移 | 长度 | 含义 |
|---|---|---|
| 0 | 8 | header，值为 `0x02` |
| 8 | 8 | 原始大小（QWORD） |
| 16 | 8 | 删除时间（FILETIME，100ns 自 1601-01-01） |
| 24 | 4 | 路径长度（宽字符数，含结尾 null） |
| 28 | 变长 | 原始完整路径，UTF-16LE，null 结尾 |

```python
import struct

with open(metadata_path, "rb") as fh:
    data = fh.read()

size, filetime = struct.unpack("<Qq", data[8:24])
(char_count,) = struct.unpack("<I", data[24:28])
original_path = data[28 : 28 + char_count * 2].decode("utf-16-le", "ignore").rstrip("\x00")
```

### 前缀匹配要同时覆盖两种形式

匹配「某个目录及其内容」时，前缀必须**同时**覆盖带尾反斜杠和不带尾反斜杠：

```python
normalised = os.path.normpath(prefix)
patterns = [normalised, normalised + os.sep]
```

- `...\projects\cache-jobs\...` → 目录**内容**
- `...\projects\cache-jobs` → 目录**本身**

**只匹配带尾斜杠的形式，会漏掉「项目根目录」那一条。** 这个坑很隐蔽：条目数看起来对（内容都删了），但回收站里还留着一个目录壳占着空间。

> 实测中正是这样漏掉的：内容条目都匹配上了，根目录那条没匹配上，导致回收站里剩一个空壳。

---

## ⚠️ 坑二：8.3 短名会让前缀匹配完全失效

这条比上一条更凶，因为**它让整个清理流程静默失效，且不报任何错**。

### 现象

```
扫描前缀 C:\Users\LONGPATH~1.USR\AppData\Local\Temp\tmpXXXX\victim
→ 0 条条目
```

但那个目录确实删掉了，回收站里也确实有内容。

### 原因

Windows 写入 `$I` 元数据时，会把 **8.3 短名解析成长名**：

| | 路径 |
|---|---|
| 删除时传入 | `C:\Users\LONGPATH~1.USR\AppData\Local\Temp\tmpXXXX\victim` |
| `$I` 里存的 | `C:\Users\LongUserName.USERHOST\AppData\Local\Temp\tmpXXXX\victim` |

> 上表用的是示意名。真实场景里，形如 `<名字>.<主机名>` 的长用户名（总长超过 8 个字符
> 时）会被 Windows 压成 8.3 形式，两者指的是同一个目录，但**字符串不相等**。
> `tempfile.gettempdir()` 返回的路径经常就是这种短名形式，所以这个坑很容易撞上。

于是字符串前缀比较**全部落空**。

`tempfile` 返回的临时目录、以及任何从短名驱动器/路径派生出来的路径，都会命中这个问题。

### 后果链

```
前缀匹配失效 → scan 报 0 条 → purge 变成空操作 → 空间永远不释放
```

而且全程没有报错。你会以为清理完了。

### 对策

比较前把两侧都规范化到「长名 + 统一大小写」：

```python
def _canonical(path):
    expanded = os.path.expandvars(str(path))
    return os.path.realpath(os.path.normpath(expanded)).lower()
```

两个要点：

1. **`os.path.realpath` 能解析 8.3 短名**（实测：短名形式被还原成完整长名）。
2. **它对尾部不存在的路径同样有效** —— 这一点至关重要，因为我们要匹配的路径**已经删掉了**。实测 `realpath` 会把存在的部分解析掉，再把不存在的部分原样接上。

```python
# 示意：前半段是已存在的目录（会被解析成长名），后半段已被删除（原样保留）
os.path.realpath(r'C:\Users\LONGPATH~1.USR\AppData\Local\Temp\tmp_gone\victim')
# → 'C:\Users\LongUserName.USERHOST\AppData\Local\Temp\tmp_gone\victim'
```

`os.path.normpath` **不能**解析短名，别用它。

### 复现与验证

删除一个位于 `tempfile.gettempdir()` 下的目录，然后用同一个路径去 scan：

```python
import os, tempfile, sys
sys.path.insert(0, "scripts")
import sh_delete as sh

with tempfile.TemporaryDirectory() as work:
    target = os.path.join(work, "victim")
    os.makedirs(target)
    print(target)                                  # 会带 ~1 之类的短名
    sh.sh_delete([os.path.abspath(target)], to_recycle=True)
    print(len(sh.scan_recycle_bin(target)))        # 修复前是 0，修复后 > 0
```

仓库里的 `tests/readme_examples_test.py` 带了这条回归检查，跑一次就能确认。

---

## 返回值不可靠

`SHFileOperationW` 的返回码 **0、2、120(0x78)、124(0x7C) 都可能是成功**（实测删掉一个目录树后返回 `2`，而操作完全正确）。不要用它判断结果。

**唯一可信的判据**：删除后 `not os.path.exists(path)`。

```python
res = sh_delete(batch, to_recycle=False)
for item in batch:
    if os.path.exists(item):
        record_failure(item)      # 这才叫失败
```

---

## 性能与操作纪律

| 事实 | 数字 |
|---|---|
| 删除 616 MB 的 `node_modules` | **76 ~ 100 秒** |
| 回收站条目数（该目录展开后） | **7511 条** |
| `ls -lat` 扫描上万条目的 `$Recycle.Bin` | **卡死超时** |
| Python `os.listdir` 扫描同一目录 | **0.01 秒** |

由此得出几条纪律：

- **长时间删除必须放后台**。前台超时会被 SIGTERM 杀掉，连已经打印的输出都会丢失。用 `print(..., flush=True)` 配合增量写 JSON 报告，保证中断也有痕迹。
- **不要用 `ls` 扫回收站**。用 `os.listdir`。
- **分批 + 逐批核验**。每批 ≤ 50 个顶层项（大目录按逻辑单元切），删完立刻验，失败即停。
- **防呆**：清回收站的脚本必须校验目标是 `$I`/`$R` 开头**且**位于当前用户的回收站目录内。对用户的历史条目一律不动，只删「可证明属于本次任务」的条目。

---

## 标准流程

```
1. 只读侦察     全盘定位目标 → 列清单 → 人工确认（含保留项、彻底程度）
2. 方式选择     走回收站（安全可恢复）还是永久删除（释放空间）
3. 分批执行     manifest → 分批 → SHFileOperationW → 逐批 os.path.exists 核验
4. 回收站核对   扫描回收站 → 按原路径前缀找本次条目 → 精确清除 → 复查剩余为 0
5. 收尾        临时探测文件用「单文件删除」清掉（不留条目）→ 输出报告
```

第 4 步不能省。省了它，第 2 步选「永久删除」就没有意义。
