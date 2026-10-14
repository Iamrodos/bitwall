# The least a receiver can be, on Windows: a wall on this console, as
# bitwall-shell is one on a POSIX terminal and PROTOCOL.md has it. Makes
# the wall's door and its sessions as named pipes under
# `//./pipe/bitwall-<user>` (WINDOWS.md), says its area from the console's
# size, and copies the base's stream to the console, where the APC
# commands pass through unseen. One layer, the base, and up to four
# controllers, each of which may ask `sessions`, `report` and `ping`. The
# wall takes the console's alternate screen with the cursor hidden, and
# gives both back when it ends; when the console is resized the base is
# told its new area and asked to repaint. Ctrl+C ends it.
#
#     powershell -File bitwall\receivers\shell\bitwall-ps.ps1 [wall]    wall 0 unless named
#
# BITWALL_RUN puts the walls elsewhere, a `//./pipe/` path. BITWALL_FONT is
# the size the area is said to be drawn at, 14 unless set, since a console
# cannot say.
#
# Windows PowerShell 5.1 makes no named pipe with the flags a wall needs
# (first instance, remote clients refused, `out` written without waiting),
# so the pipes are the calls bitwall/local.py makes, in C#: a thread a pipe
# the wall reads, one piece in hand at a time, so a wall that stops reading
# fills the pipe and its writer waits.
param([string]$Wall = '0')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

if (-not ('BitwallPs.W' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace BitwallPs {

public static class W {
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern IntPtr CreateNamedPipeW(string name, uint openMode, uint pipeMode,
        uint instances, uint outBuffer, uint inBuffer, uint timeout, IntPtr sa);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool ConnectNamedPipe(IntPtr h, IntPtr ov);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool ReadFile(IntPtr h, IntPtr buf, uint n, IntPtr read, IntPtr ov);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool WriteFile(IntPtr h, byte[] buf, uint n, out uint written, IntPtr ov);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetOverlappedResult(IntPtr h, IntPtr ov, out uint n, bool wait);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CancelIoEx(IntPtr h, IntPtr ov);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr h);
    [DllImport("kernel32.dll")]
    public static extern ulong GetTickCount64();
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl,
        uint revision, out IntPtr sd, IntPtr size);

    public static readonly IntPtr Invalid = new IntPtr(-1);
    public const uint Inbound = 0x1, Outbound = 0x2, Overlapped = 0x40000000, First = 0x80000;
    public const uint NoWait = 0x1, LocalOnly = 0x8;   // PIPE_REJECT_REMOTE_CLIENTS
    public static IntPtr Sa = IntPtr.Zero;

    /// Every pipe made with this security: its user, the administrators
    /// and the system, whether the receiver runs elevated or not.
    public static void Secure(string sddl) {
        IntPtr sd;
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, out sd, IntPtr.Zero))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        Sa = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(IntPtr)) * 3);
        Marshal.WriteInt32(Sa, 0, IntPtr.Size == 8 ? 24 : 12);
        Marshal.WriteIntPtr(Sa, IntPtr.Size, sd);
        Marshal.WriteInt32(Sa, IntPtr.Size * 2, 0);
    }
}

/// A wall's pipe that it reads: `in`, or one of the door's instances. A
/// thread connects and reads it overlapped, a megabyte at a time, and
/// hands over one piece at a time; end of file is said once per writer,
/// and the next writer's instance is made before this one goes. `Whole`
/// keeps a caller's bytes until it closes, so door requests never mix.
public class Reader {
    public static readonly AutoResetEvent Bell = new AutoResetEvent(false);
    const int Size = 1 << 20;
    readonly string name; readonly uint buffer, instances; readonly bool whole;
    readonly object lk = new object();
    readonly Queue<byte[]> pieces = new Queue<byte[]>();
    readonly ManualResetEvent ev = new ManualResetEvent(false), stop = new ManualResetEvent(false);
    int eofs; volatile bool closed; IntPtr h, ov, buf;

    public Reader(string name, uint buffer, uint instances, bool whole, bool first) {
        this.name = name; this.buffer = buffer; this.instances = instances; this.whole = whole;
        h = Make(first);
        if (h == W.Invalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        ov = Marshal.AllocHGlobal(64); buf = Marshal.AllocHGlobal(Size);
        var t = new Thread(Run); t.IsBackground = true; t.Start();
    }

    IntPtr Make(bool first) {
        return W.CreateNamedPipeW(name, W.Inbound | W.Overlapped | (first ? W.First : 0),
                                  W.LocalOnly, instances, 4096, buffer, 0, W.Sa);
    }

    void Arm() {
        ev.Reset();
        var o = new NativeOverlapped(); o.EventHandle = ev.SafeWaitHandle.DangerousGetHandle();
        Marshal.StructureToPtr(o, ov, false);
    }

    /// An overlapped call's result as an error code, 0 for done; given up
    /// when the reader is closed.
    int Finish(bool done, out uint n) {
        n = 0;
        if (!done) {
            int e = Marshal.GetLastWin32Error();
            if (e != 997) return e;                       // not ERROR_IO_PENDING
            if (WaitHandle.WaitAny(new WaitHandle[] { ev, stop }) == 1) W.CancelIoEx(h, ov);
        }
        return W.GetOverlappedResult(h, ov, out n, true) ? 0 : Marshal.GetLastWin32Error();
    }

    void Push(byte[] data, bool eof) {
        lock (lk) {
            while (pieces.Count > 0 && !closed) Monitor.Wait(lk, 500);
            if (data != null) pieces.Enqueue(data);
            if (eof) eofs++;
        }
        Bell.Set();
    }

    void Run() {
        uint n; int e;
        try {
            while (!closed) {
                Arm(); e = Finish(W.ConnectNamedPipe(h, ov), out n);
                if (closed) break;
                if (e != 0 && e != 535 && e != 232) break;    // connected, or came and went
                IntPtr next = Make(false);
                var held = new List<byte[]>(); int heldN = 0;
                while (!closed) {
                    Arm(); e = Finish(W.ReadFile(h, buf, Size, IntPtr.Zero, ov), out n);
                    if (e != 0) break;                         // broken: its writer went
                    if (n == 0) continue;
                    var data = new byte[n]; Marshal.Copy(buf, data, 0, (int)n);
                    if (!whole) Push(data, false);
                    else if (heldN < 4096) { held.Add(data); heldN += data.Length; }
                }
                if (!closed) {
                    if (!whole) Push(null, true);
                    else if (heldN > 0) {
                        var all = new byte[heldN]; int at = 0;
                        foreach (var p in held) { Buffer.BlockCopy(p, 0, all, at, p.Length); at += p.Length; }
                        Push(all, false);
                    }
                }
                W.CloseHandle(h); h = next;
                if (h == W.Invalid) return;
            }
        } finally {
            if (h != W.Invalid) W.CloseHandle(h);
            h = W.Invalid;
            Marshal.FreeHGlobal(buf); Marshal.FreeHGlobal(ov);
            Bell.Set();
        }
    }

    /// The next piece, an empty one at a writer's end of file, or null
    /// when nothing has come.
    public byte[] Take() {
        lock (lk) {
            if (pieces.Count > 0) { var p = pieces.Dequeue(); Monitor.PulseAll(lk); return p; }
            if (eofs > 0) { eofs--; Monitor.PulseAll(lk); return new byte[0]; }
            return null;
        }
    }

    public void Close() {
        closed = true; stop.Set();
        lock (lk) Monitor.PulseAll(lk);
    }
}

/// A wall's door: four instances, so callers at once each have one, the
/// first made first of its name, which is the claim.
public class Door {
    readonly Reader[] ends = new Reader[4];
    public Door(string name) {
        for (int i = 0; i < ends.Length; i++) ends[i] = new Reader(name, 4096, 8, true, i == 0);
    }
    public string Take() {
        var sb = new StringBuilder();
        foreach (var e in ends) {
            byte[] p;
            while ((p = e.Take()) != null) sb.Append(Encoding.UTF8.GetString(p));
        }
        return sb.ToString();
    }
    public void Close() { foreach (var e in ends) e.Close(); }
}

/// `out`: written without waiting (PIPE_NOWAIT). What the wall says
/// before its reader comes, or while the pipe is full, waits here and
/// goes on the next `Flush`; once written it outlives the handle.
public class Out {
    IntPtr h; readonly List<byte> queue = new List<byte>();
    public bool Connected, Gone;
    public Out(string name) {
        h = W.CreateNamedPipeW(name, W.Outbound, W.NoWait | W.LocalOnly, 1, 1 << 16, 4096, 0, W.Sa);
        if (h == W.Invalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    public void Say(string text) { if (!Gone) queue.AddRange(Encoding.UTF8.GetBytes(text)); Flush(); }
    public bool Empty { get { return queue.Count == 0; } }

    public void Flush() {
        while (queue.Count > 0 && h != W.Invalid && !Gone) {
            int take = Math.Min(queue.Count, 1 << 16);
            var chunk = queue.GetRange(0, take).ToArray();
            uint n;
            if (!W.WriteFile(h, chunk, (uint)take, out n, IntPtr.Zero)) {
                int e = Marshal.GetLastWin32Error();
                if (e == 232 || e == 109) { Gone = true; queue.Clear(); }
                return;                                   // 536: no reader yet
            }
            Connected = true;
            if (n == 0) return;                           // full
            queue.RemoveRange(0, (int)n);
        }
    }

    /// Whether its reader has come and gone.
    public bool Left() {
        if (Gone || h == W.Invalid) return true;
        if (!W.ConnectNamedPipe(h, IntPtr.Zero)) {
            int e = Marshal.GetLastWin32Error();
            if (e == 232 || e == 109) Gone = true;
        }
        return Gone;
    }

    public void Close() { if (h != W.Invalid) { W.CloseHandle(h); h = W.Invalid; } }
}

/// The console: its modes, kitty's question, and Ctrl+C as a key.
public static class Con {
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int n);
    [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr h, out uint m);
    [DllImport("kernel32.dll")] static extern bool SetConsoleMode(IntPtr h, uint m);
    [DllImport("kernel32.dll")] static extern bool GetNumberOfConsoleInputEvents(IntPtr h, out uint n);
    [DllImport("kernel32.dll")] static extern bool ReadConsoleInputW(IntPtr h, byte[] buf, uint len, out uint read);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool WriteFile(IntPtr h, byte[] buf, uint n, out uint written, IntPtr ov);

    static IntPtr In { get { return GetStdHandle(-10); } }
    static IntPtr OutH { get { return GetStdHandle(-11); } }
    static uint inMode, outMode;

    /// Input as VT sequences with Ctrl+C a key, output as VT; `Restore`
    /// gives both back.
    public static void Take() {
        GetConsoleMode(In, out inMode); GetConsoleMode(OutH, out outMode);
        SetConsoleMode(In, 0x200);                        // ENABLE_VIRTUAL_TERMINAL_INPUT
        SetConsoleMode(OutH, outMode | 0x1 | 0x4);        // processed, VT processing
    }
    public static void Restore() { SetConsoleMode(In, inMode); SetConsoleMode(OutH, outMode); }

    /// The characters of every key pressed since last asked.
    static string Pending() {
        var sb = new StringBuilder(); uint n, got; var rec = new byte[20 * 64];
        while (GetNumberOfConsoleInputEvents(In, out n) && n > 0) {
            if (!ReadConsoleInputW(In, rec, 64, out got)) break;
            for (int i = 0; i < got; i++) {
                int at = i * 20;
                if (BitConverter.ToUInt16(rec, at) == 1 && BitConverter.ToInt32(rec, at + 4) != 0) {
                    char c = BitConverter.ToChar(rec, at + 14);
                    int repeat = Math.Max(1, (int)BitConverter.ToUInt16(rec, at + 8));
                    if (c != 0) sb.Append(c, repeat);
                }
            }
        }
        return sb.ToString();
    }

    /// What the console answers to `query`, up to the device attributes
    /// every terminal answers, or what came in `ms`.
    public static string Ask(string query, int ms) {
        Pending();
        var q = Encoding.ASCII.GetBytes(query); uint w;
        WriteFile(OutH, q, (uint)q.Length, out w, IntPtr.Zero);
        var said = new StringBuilder(); ulong until = W.GetTickCount64() + (ulong)ms;
        while (W.GetTickCount64() < until) {
            WaitForSingleObject(In, 20);
            said.Append(Pending());
            var s = said.ToString(); int da = s.IndexOf("\x1b[?");
            if (da >= 0 && s.IndexOf('c', da) > 0) break;
        }
        return said.ToString();
    }

    /// Whether Ctrl+C was pressed; every other key is let go.
    public static bool Interrupted() { return Pending().IndexOf('\x03') >= 0; }
}
}
'@ }

$ESC = [char]27
$utf8 = New-Object System.Text.UTF8Encoding($false)

$runDir = if ($env:BITWALL_RUN) { $env:BITWALL_RUN } else { "//./pipe/bitwall-$($env:USERNAME.ToLower())" }
$wallDir = "$runDir/bitwall/$Wall"
$font = if ($env:BITWALL_FONT) { [int]$env:BITWALL_FONT } else { 14 }
$caps = 'caps area sessions report ping layers=1 types=cells controllers=4'
function Native([string]$path) { $path.Replace('/', '\') }

if ($Wall -notmatch '^[A-Za-z0-9_-]{1,32}$') { [Console]::Error.WriteLine("bitwall-ps: $Wall`: not a wall's name"); exit 1 }
if ($runDir -notlike '//./pipe/*') { [Console]::Error.WriteLine("bitwall-ps: $runDir`: walls on Windows are pipes, //./pipe/<name>"); exit 1 }

# Which program keeps the wall, as a report says it: this script, its
# version a hash of itself as bitwall/receivers/__init__.py works it out,
# when it started and when its machine did.
$ver = (Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.Substring(0, 12).ToLower()
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$booted = $now - [long]([BitwallPs.W]::GetTickCount64() / 1000)
$rcv = "receiver bitwall-ps version=$ver started=$now booted=$booted"

try { $cols = [Console]::WindowWidth; $rows = [Console]::WindowHeight } catch { $cols = 0 }
if (-not $cols) { [Console]::Error.WriteLine('bitwall-ps: no console'); exit 1 }

$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
[BitwallPs.W]::Secure("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;$me)")
try { $door = New-Object BitwallPs.Door (Native "$wallDir/door.in") }
catch {
    [Console]::Error.WriteLine("bitwall-ps: $wallDir`: its door is read")
    exit 1
}

[Console]::OutputEncoding = $utf8
$tty = [Console]::OpenStandardOutput()
function Paint([string]$text) { $b = $utf8.GetBytes($text); $tty.Write($b, 0, $b.Length); $tty.Flush() }
[BitwallPs.Con]::Take()
# Whether the console draws kitty graphics, which a sender then sends
# through, untouched, for it to draw: kitty's own question, a one-pixel
# image asked about and not drawn, then the device attributes every
# terminal answers. The graphics reply before the attributes is a yes.
$said = [BitwallPs.Con]::Ask("${ESC}_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA$ESC\$ESC[c", 500)
if ($said.Contains('_Gi=31;OK')) { $caps += ' kitty' }

# The status page, while no sender is on the wall, as the painter's: what
# this display is, where to send to, its area, and when a sender was last
# seen, in the painter's colours, a block in the middle of the screen.
$hostName = [Net.Dns]::GetHostName()
$addr = [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
    Where-Object { $_.OperationalStatus -eq 'Up' -and $_.GetIPProperties().GatewayAddresses.Count } |
    ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
    Where-Object { $_.Address.AddressFamily -eq 'InterNetwork' } |
    Select-Object -First 1 | ForEach-Object { $_.Address.ToString() }
$lastSeen = 'never this run'
function Status {
    $x = [Math]::Max(0, [int][Math]::Floor(($cols - 60) / 2)); $y = [Math]::Max(0, [int][Math]::Floor(($rows - 11) / 2))
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("$ESC[0m$ESC[H$ESC[2J")
    $lines = @(
        '',
        "   $ESC[1;35m  bitwall$ESC[22;39m   $ESC[38;5;240mdisconnected$ESC[39m",
        '',
        (Pair 'this display' "$hostName  $addr"),
        (Pair 'wall' $Wall),
        (Pair 'send to' "$($hostName.ToLower()):$Wall"),
        (Pair 'font size' $font),
        (Pair 'area' "${cols}x$rows cells"),
        (Pair 'stream' 'no sender connected' "$ESC[1;31m"),
        (Pair 'last seen' $lastSeen),
        '',
        "   $ESC[38;5;240m$((Get-Date).ToString('ddd dd MMM HH:mm'))$ESC[39m")
    foreach ($l in $lines) { $y++; [void]$sb.Append("$ESC[$y;$($x + 1)H$l") }
    Paint $sb.ToString()
}
function Pair($k, $v, $c = "$ESC[38;5;250m") { "   $ESC[38;5;244m{0,-14}$ESC[22;39m {1}{2}$ESC[22;39m" -f $k, $c, $v }

# The wall's sessions by name: the base, its controllers, and those done
# with but whose `out` still has something for a reader.
$base = $null
$ctls = [ordered]@{}
$closing = New-Object System.Collections.ArrayList
function Session($name) {
    $d = "$wallDir/s/$name"
    $in = New-Object BitwallPs.Reader (Native "$d/in"), (1 -shl 20), 2, $false, $false
    try { $out = New-Object BitwallPs.Out (Native "$d/out") } catch { $in.Close(); throw }
    [pscustomobject]@{ Name = $name; In = $in; Out = $out; Inst = ''; Rest = ''; Until = 0 }
}
function Taken($name) { ($base -and $base.Name -eq $name) -or $ctls.Contains($name) -or @($closing | Where-Object Name -eq $name).Count }
function Done($s) {
    $s.In.Close(); $s.Until = [BitwallPs.W]::GetTickCount64() + 10000
    [void]$closing.Add($s)
}
# A line every controller hears, as the base opens and closes.
function Tell($line) { foreach ($c in $ctls.Values) { $c.Out.Say("$line`n") } }

function EndBase($why) {
    $s = $script:base; $script:base = $null
    $s.Out.Say("closed $($s.Name) $why`n"); Done $s
    Tell "session closed $($s.Name) $why"
    if ($why -eq 'eof') { $script:lastSeen = (Get-Date).ToString('ddd dd MMM HH:mm:ss'); Status }
}

# A controller's commands are APCs on `in`, split at each ESC, and its
# answers lines on `out`, each carrying the command's id when it had one.
function Command($c, $rec) {
    if (-not $rec.StartsWith('_bitwall ')) { return $true }
    $w = @($rec.Substring(9).Split(' ', [StringSplitOptions]::RemoveEmptyEntries))
    if (-not $w.Count) { return $true }
    $verb = $w[0]; $id = ''
    foreach ($x in $w) { if ($x.StartsWith('id=')) { $id = $x.Substring(3) } }
    $tail = if ($id) { " id=$id" } else { '' }
    $say = { param($t) $c.Out.Say("$t$tail`n") }
    switch ($verb) {
        'sessions' {
            $all = @(); if ($base) { $all += $base }; $all += @($ctls.Values)
            foreach ($s in ($all | Sort-Object Name)) {
                $iv = if ($s.Inst) { " instance=$($s.Inst)" } else { '' }
                if ([object]::ReferenceEquals($s, $base)) { & $say "session $($s.Name) layer 0 cells$iv" }
                else { & $say "session $($s.Name) controller - -$iv" }
            }
            & $say "sessions $($all.Count)"
        }
        'report' { & $say 'hello v1 line'; & $say $rcv; & $say $caps; & $say "area ${cols}x$rows $font" }
        'ping' { & $say 'pong' }
        'close' { return $false }
        'detach' { return $false }
        'pong' { }
        default { & $say "error $verb unknown command" }
    }
    $true
}
function EndCtl($c) {
    $ctls.Remove($c.Name); $c.Out.Say("closed $($c.Name) eof`n"); Done $c
}

# A request at the door: `open <name> layer|controller [key=value...]`.
function Open($line) {
    $f = @($line.Trim().Split(' ', [StringSplitOptions]::RemoveEmptyEntries))
    if ($f.Count -lt 2 -or $f[0] -ne 'open') { return }
    $name = $f[1]; $role = if ($f.Count -gt 2) { $f[2] } else { '' }
    $opts = if ($f.Count -gt 3) { $f[3..($f.Count - 1)] } else { @() }
    if ($name -cnotmatch '^[A-Za-z0-9_-]{1,32}$' -or $name -eq 'glass' -or (Taken $name)) { return }
    foreach ($o in $opts) { if ($o -notmatch '^.+=.+$') { return } }
    $reason = ''; $take = $false; $full = ''; $inst = ''
    function Inst($o) {
        $v = $o.Substring(9)
        if ($v.StartsWith('-') -or $v -cmatch '[^a-z0-9-]' -or $v.Length -gt 64) { return $false }
        $script:inst = $v; $true
    }
    switch ($role) {
        'layer' {
            foreach ($o in $opts) {
                if ($o -eq 'z=0' -or $o -eq 'type=cells') { }
                elseif ($o -eq 'take=1') { $take = $true }
                elseif ($o -match '^z=.*[^0-9]') { if (-not $reason) { $reason = 'no-such-key' } }
                elseif ($o.StartsWith('instance=')) { if (-not (Inst $o) -and -not $reason) { $reason = 'no-such-key' } }
                elseif ($o.StartsWith('z=')) { $full = 'full' }
                elseif ($o.StartsWith('type=')) { if (-not $reason) { $reason = 'no-such-type' } }
                elseif (-not $reason) { $reason = 'no-such-key' }
            }
            if (-not $reason) { $reason = $full }
            if (-not $reason -and $base -and -not $take) { $reason = 'busy' }
        }
        'controller' {
            foreach ($o in $opts) {
                if ($o.StartsWith('instance=')) { if (-not (Inst $o) -and -not $reason) { $reason = 'no-such-key' } }
                elseif (-not $reason) { $reason = 'no-such-key' }
            }
            if (-not $reason -and $ctls.Count -ge 4) { $reason = 'full' }
        }
        default { $reason = 'no-such-role' }
    }
    try { $s = Session $name } catch { return }
    $s.Inst = $script:inst; $script:inst = ''
    if ($reason) { $s.Out.Say("hello v1 line`nerror open $name $reason`n"); Done $s; return }
    $iv = if ($s.Inst) { " instance=$($s.Inst)" } else { '' }
    if ($role -eq 'controller') {
        $s.Out.Say("hello v1 line`nopened $name`n$caps`n")
        $ctls[$name] = $s
        return
    }
    if ($base) { EndBase 'displaced' }
    $s.Out.Say("hello v1 line`nopened $name z=0`n$caps`narea ${cols}x$rows $font`n")
    $script:base = $s
    Tell "session opened $name layer 0 cells$iv"
}
$inst = ''

Paint "$ESC[?1049h$ESC[?25l"
Status
try {
    while ($true) {
        [void][BitwallPs.Reader]::Bell.WaitOne(50)
        if ([BitwallPs.Con]::Interrupted()) { break }

        # The console's size again; the base, if there is one, is told and repaints.
        $c2 = [Console]::WindowWidth; $r2 = [Console]::WindowHeight
        if ($c2 -ne $cols -or $r2 -ne $rows) {
            $cols = $c2; $rows = $r2; Paint "$ESC[H$ESC[2J"
            if ($base) { $base.Out.Say("area ${cols}x$rows $font`nrepaint`n") } else { Status }
        }

        foreach ($line in $door.Take().Split("`n")) { if ($line.Trim()) { Open $line } }

        if ($base) {
            while ($base -and $null -ne ($p = $base.In.Take())) {
                if ($p.Length -eq 0) { EndBase 'eof' } else { $tty.Write($p, 0, $p.Length) }
            }
            $tty.Flush()
            if ($base) { $base.Out.Flush() }
        }

        foreach ($c in @($ctls.Values)) {
            $ended = $false
            while (-not $ended -and $null -ne ($p = $c.In.Take())) {
                $text = $c.Rest + $utf8.GetString($p)
                $recs = $text.Split($ESC)
                if ($p.Length -eq 0) { $c.Rest = '' } else { $c.Rest = $recs[-1]; $recs = if ($recs.Count -gt 1) { $recs[0..($recs.Count - 2)] } else { @() } }
                foreach ($rec in $recs) { if (-not (Command $c $rec)) { $ended = $true; break } }
                if ($p.Length -eq 0) { $ended = $true }
            }
            if ($ended) { EndCtl $c } else { $c.Out.Flush() }
        }

        # A session done with is let go once its reader has had what it was
        # told, or has gone, or ten seconds on with no reader.
        foreach ($s in @($closing)) {
            $s.Out.Flush()
            if ($s.Out.Gone -or ($s.Out.Empty -and $s.Out.Connected) -or [BitwallPs.W]::GetTickCount64() -gt $s.Until) {
                $s.Out.Close(); $closing.Remove($s)
            }
        }
    }
} finally {
    $door.Close()
    foreach ($s in @(@($base) + @($ctls.Values) + @($closing))) { if ($s) { $s.In.Close(); $s.Out.Close() } }
    Paint "$ESC[0m$ESC[?25h$ESC[?1049l"
    [BitwallPs.Con]::Restore()
}
