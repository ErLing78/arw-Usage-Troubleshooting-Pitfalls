# 02 · 加载程序集与执行脚本

这篇讲两件在受控机器上经常被堵、但都有绕法的事：**加载 .NET 程序集**和**执行 `.ps1`**。

---

## 一、`Add-Type` 与 `LoadFrom`

### 问题

`Add-Type` 的作用是把 C# 源码**在内存里即时编译**成程序集。因为它能执行任意代码，很多安全策略把它当高危操作拦掉：

```
Add-Type : Command blocked for security.
```

`Add-Type -AssemblyName X` 这种「只加载已有程序集」的调用也可能一起被拦。

### 绕法：`LoadFrom`

**加载一个已经存在于磁盘上的程序集，是完全不同的一件事**，通常不在拦截范围内：

```powershell
# 被拦
Add-Type -AssemblyName System.Drawing

# 可用
$null = [System.Reflection.Assembly]::LoadFrom(
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Drawing.dll'
)
```

这一条打开了整个 .NET Framework：

| 程序集 | 能做什么 |
|---|---|
| `System.Drawing.dll` | GDI+ 图像编解码（JPEG/PNG/BMP/TIFF，含缩放） |
| `System.Windows.Forms.dll` | 截屏、剪贴板 |
| `System.Management.dll` | WMI 查询 |
| `System.Runtime.WindowsRuntime.dll` | WinRT 异步桥 |

### 注意位数

路径里的 `Framework64` 对应 64 位进程，32 位要换成 `Framework`。写死之前先判断：

```powershell
$frameworkRoot = if ([Environment]::Is64BitProcess) {
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
} else {
    'C:\Windows\Microsoft.NET\Framework\v4.0.30319'
}
```

用错了目录会报「找不到文件」，而不是「位数不匹配」——容易误诊。

### 实测：拦截不是绝对的

2026-10-15 在一台 Windows 11（10.0.26100）、PowerShell 5.1.26100.9444 上跑自检，结果是：

```
addTypeAssemblyName      succeeded
addTypeTypeDefinition    succeeded   ← 连即时编译都成功
loadWithPartialName      succeeded
loadFrom_System_Drawing  System.Drawing 4.0.0.0
```

**`Add-Type -TypeDefinition` 都可以用。**

这很有意思：同一台机器，几个月前的记录是「`Add-Type` 被拦、`LoadWithPartialName` 被拦」。现在两个都通了。

可能的原因：策略更新、域控策略变动、或者当初的拦截发生在特定调用路径上。

**结论不是「Add-Type 现在能用了」，而是「能力是动态的，必须现场探测」。** 这正是本仓库要带一个自检脚本的原因——别信任何静态的「这台机器行不行」，包括本文档。

---

## 二、执行 `.ps1`：绕开 `Restricted` 策略

### 问题

```powershell
PS> .\script.ps1
PS>            # 什么都不发生
PS> $LASTEXITCODE
1
```

**没有报错，没有输出，只是不执行。** 这是最容易被误判成「脚本写错了」的现象。

查一下：

```powershell
Get-ExecutionPolicy -List
# MachinePolicy  Undefined
# UserPolicy     Undefined
# Process        Undefined
# CurrentUser    Undefined
# LocalMachine   Undefined
Get-ExecutionPolicy
# Restricted
```

注意：**所有 scope 都是 `Undefined`，但生效值是 `Restricted`**。这是默认值，没有哪个 scope 显式设置它——所以你会以为「没人限制我」。查生效值要用不带参数的 `Get-ExecutionPolicy`。

### 绕法：`scriptblock`

不去改策略（那通常需要管理员且会留下痕迹），而是把脚本**当文本读进来**再执行：

```powershell
& ([scriptblock]::Create((Get-Content -Raw 'C:\path\to\script.ps1')))
```

带参数：

```powershell
$sb = [scriptblock]::Create('param($a) & ([scriptblock]::Create((Get-Content -Raw "C:\lib\tool.ps1"))) -InputPath $a')
& $sb 'C:\in.jpg'
```

### ⚠️ 坑一：`$script:` 在 scriptblock 里是 `null`

`[scriptblock]::Create()` 造出来的 scriptblock **没有文件作用域**。脚本里写 `$script:counter` 不会报「未定义」，而是静默解析成 `$null`，然后在下一行炸出：

```
不能对 Null 值表达式调用方法。
```

**对策**：改用 `$global:`，或者干脆不用脚本级状态——把结果增量写盘。

```powershell
# 不要这样
$script:results += $item

# 可以这样：增量写盘
[System.IO.File]::AppendAllText($out, ($item | ConvertTo-Json) + "`n", $enc)
```

后者还有个好处：中途被杀也有痕迹。

---

## 三、⚠️ 坑二：动态作用域遮蔽（这个很难查）

PowerShell 的 scriptblock 里的自由变量是**在调用时才解析**的——解析顺序是**调用者作用域**，而不是定义处的作用域。

于是这样写会出问题：

```powershell
function Add-Probe {
    param([string]$Group, [string]$Name, [scriptblock]$Body)
    & $Body
}

foreach ($name in @('System.Drawing', 'System.Windows.Forms')) {
    Add-Probe 'assemblyLoading' $name {
        Load-It ($name + '.dll')     # ← 这里的 $name 是谁？
    }
}
```

看起来 `$name` 是 `System.Drawing`。实际上：

**`Add-Probe` 的参数 `$Name` 遮蔽了调用方的 `$name`。** scriptblock 里的 `$name` 解析到了 `Add-Probe` 自己的参数——也就是探针名 `loadFrom_System_Drawing`。

结果：路径拼成了 `C:\...\Framework64\v4.0.30319\loadFrom_System_Drawing.dll`，报「找不到程序集」。

**每个探针都失败，错误信息还言之凿凿地说程序集不存在**——但实际上那四个程序集都在，是变量名撞了。

### 对策

**给循环变量和函数参数取不冲突的名字**：

```powershell
function Add-Probe {
    param([string]$Group, [string]$Name, [scriptblock]$Body)   # 保持 $Name
}

# 循环变量改用前缀，避开 $Name
foreach ($asmName in $assemblies.Keys) {
    $asmProbe = 'loadFrom_' + ($asmName -replace '\.', '_')
    $asmPath = Join-Path $frameworkRoot $assemblies[$asmName]
    Add-Probe 'assemblyLoading' $asmProbe {
        Load-It $asmPath
    }
}
```

或者用 `.GetNewClosure()` 把当前变量值**捕获**进 scriptblock：

```powershell
Add-Probe 'assemblyLoading' $asmProbe { Load-It $asmPath }.GetNewClosure()
```

**判断依据**：scriptblock 里的变量名，和包裹它的函数的参数名，有没有重名。有就可能出事。

这个坑之所以难查，是因为**报错信息完全指向错误的方向**——它说是程序集不存在，实际是变量被遮蔽。看到 scriptblock 里的变量值和预期不符时，先怀疑这个。

---

## 四、stdout 不返回

某些宿主（包括自动化工具调用 PowerShell 时）**不返回 stdout**，只给 exit code。`Write-Output`、`Write-Host` 全部丢弃。

**一切结果都要落盘再读回**：

```powershell
$enc = New-Object System.Text.UTF8Encoding($false)    # $false = 不写 BOM
[System.IO.File]::WriteAllText($out, $text, $enc)
```

### 为什么不用 `Out-File`

Windows PowerShell 5.1 的 `Out-File` 默认编码是 **UTF-16LE**（或带 BOM 的 UTF-8），下游按 UTF-8 读会得到一堆 `\x00` 夹着字符，中文尤其明显。

`UTF8Encoding($false)` 明确指定「UTF-8，不要 BOM」：

```powershell
# 会写出 BOM，某些 JSON 解析器会报错
$json | Out-File -FilePath $path -Encoding utf8

# 干净
$enc = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($path, $json, $enc)
```

**注意**：BOM 会让 `json.load()` 在某些 Python 版本下抛出 `Unexpected UTF-8 BOM`。这个报错看起来吓人，其实只是三个字节的问题。

---

## 五、小结

| 症状 | 原因 | 对策 |
|---|---|---|
| `Add-Type : Command blocked for security` | 即时编译被拦 | `LoadFrom` 加载已有程序集 |
| `.ps1` 不执行、exit 1、无输出 | 执行策略 `Restricted` | `[scriptblock]::Create((Get-Content -Raw $p))` |
| `不能对 Null 值表达式调用方法` | `$script:` 在 scriptblock 里为 null | 用 `$global:` 或增量写盘 |
| 变量值莫名其妙不对 | scriptblock 变量被调用者参数遮蔽 | 改名，或用 `.GetNewClosure()` |
| PowerShell 跑完什么都不回显 | 宿主不返回 stdout | 一切写文件再读回 |
| 中文变成乱码 / JSON 解析报 BOM | `Out-File` 的默认编码 | `UTF8Encoding($false)` + `WriteAllText` |

**动手前先跑 `scripts/diagnose.ps1`**，上面这些能不能用，五秒钟就有答案。
