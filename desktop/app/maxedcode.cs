// maxedcode app: a native Windows window (WebView2, the Edge engine built into Windows) around maxedcode's own
// local server (agent/maxedcode/webapp). No browser is involved.
//
// It starts the server hidden (python local_agent.py --app) or reuses the one already running, shows the page,
// and on close tells the server to quit once idle: a running turn is finished first, reopening the window shows it.
//
// No exe of our own: maxedcode.ps1 compiles this file in memory (Add-Type, ~0.2 s) and calls MaxedcodeApp.Start.
// Windows' Smart App Control blocks unsigned programs it does not know, and a locally built exe is one of them;
// powershell.exe, conhost.exe and the WebView2 DLLs in lib\ (Microsoft, BSD-style license) are signed by Microsoft.
// The compiler is the .NET Framework 4 one, so C# 5 only: no string interpolation, no ?. and no => members.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

public static class MaxedcodeApp
{
    public const string AppId = "maxedcode.app";        // own taskbar button and pin, not PowerShell's
    public static string AppDir, LibDir, IconPath;
    public static bool UseExe;                            // started as maxedcode.exe (program.cs), not by maxedcode.ps1
    // Server log, URL file and the window's WebView2 profile: outside ~\.maxedcode, which sync button 2 copies into
    // the repo.
    public static readonly string Home = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".maxedcode-app");

    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("shell32.dll")] static extern int SetCurrentProcessExplicitAppUserModelID([MarshalAs(UnmanagedType.LPWStr)] string id);
    [DllImport("shell32.dll")] static extern int SHGetPropertyStoreForWindow(IntPtr hwnd, ref Guid iid, out IPropertyStore store);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);

    static void Paths(string appDir)
    {
        AppDir = Path.GetFullPath(appDir);
        LibDir = Path.Combine(AppDir, "lib");
        IconPath = Path.GetFullPath(Path.Combine(AppDir, "..", "maxedcode.ico"));
    }

    /// <summary>The window. Called by maxedcode.ps1 (powershell -STA).</summary>
    public static void Start(string appDir, int port)
    {
        Paths(appDir);
        SetProcessDpiAwarenessContext(new IntPtr(-4));  // per-monitor v2: sharp on scaled screens
        SetCurrentProcessExplicitAppUserModelID(AppId);
        Application.EnableVisualStyles();
        try { Application.SetCompatibleTextRenderingDefault(false); } catch (InvalidOperationException) { }
        Directory.CreateDirectory(Home);
        // One window per server, like a normal app: a second start brings the open window to the front.
        bool first;
        using (var single = new Mutex(true, "Local\\maxedcode.app." + port, out first))
        {
            if (!first)
            {
                FocusOpenWindow(port);
                return;
            }
            Application.Run(new MainWindow(port));
            GC.KeepAlive(single);
        }
    }

    /// <summary>What a shortcut or a taskbar pin runs: maxedcode.exe when the window is the exe, else PowerShell
    /// without any console window.</summary>
    public static string Arguments(out string target)
    {
        string exe = Path.Combine(AppDir, "maxedcode.exe");
        if (UseExe && File.Exists(exe))
        {
            target = exe;
            return "";
        }
        target = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "conhost.exe");
        return "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File \"" +
               Path.Combine(AppDir, "maxedcode.ps1") + "\"";
    }

    /// <summary>"maxedcode app" shortcuts on the desktop and in the Start menu (maxedcode.ps1 -Shortcuts).</summary>
    public static string[] Shortcuts(string appDir)
    {
        Paths(appDir);
        string target, args = Arguments(out target);
        var made = new List<string>();
        foreach (var folder in new[] { Environment.SpecialFolder.DesktopDirectory, Environment.SpecialFolder.Programs })
        {
            string path = Path.Combine(Environment.GetFolderPath(folder), "maxedcode app.lnk");
            var link = (IShellLinkW)new ShellLink();
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(AppDir);
            link.SetIconLocation(IconPath, 0);
            link.SetDescription("maxedcode in its own window");
            link.SetShowCmd(7);                           // minimized: nothing flashes up
            Props.Set((IPropertyStore)link, Props.AppUserModelId, AppId);
            ((IPersistFile)link).Save(path, true);
            Marshal.ReleaseComObject(link);
            made.Add(path);
        }
        return made.ToArray();
    }

    static string HandleFile(int port) { return Path.Combine(Home, "window-" + port + ".hwnd"); }

    public static void SaveHandle(int port, IntPtr hwnd)
    {
        try { File.WriteAllText(HandleFile(port), hwnd.ToInt64().ToString()); } catch (IOException) { }
    }

    static void FocusOpenWindow(int port)
    {
        long value;
        string text;
        try { text = File.ReadAllText(HandleFile(port)).Trim(); } catch (IOException) { return; }
        if (!long.TryParse(text, out value)) return;
        var hwnd = new IntPtr(value);
        if (!IsWindow(hwnd)) return;
        if (IsIconic(hwnd)) ShowWindow(hwnd, 9);          // SW_RESTORE
        SetForegroundWindow(hwnd);
    }

    /// <summary>A taskbar pin of the window gets maxedcode's name, icon and command, not powershell.exe's.</summary>
    public static void TagWindow(IntPtr hwnd)
    {
        try
        {
            Guid iid = typeof(IPropertyStore).GUID;
            IPropertyStore store;
            if (SHGetPropertyStoreForWindow(hwnd, ref iid, out store) != 0) return;
            string target, args = Arguments(out target);
            Props.Set(store, Props.AppUserModelId, AppId);
            Props.Set(store, Props.RelaunchCommand, "\"" + target + "\" " + args);
            Props.Set(store, Props.RelaunchIcon, IconPath);
            Props.Set(store, Props.RelaunchName, "maxedcode app");
            Marshal.ReleaseComObject(store);
        }
        catch (Exception) { }
    }
}

// ── Shell interop: shortcuts and the window's AppUserModel properties ──
[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
class ShellLink { }

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
interface IShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int max, IntPtr data, int flags);
    void GetIDList(out IntPtr list);
    void SetIDList(IntPtr list);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int max);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int max);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int max);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
    void GetHotkey(out short key);
    void SetHotkey(short key);
    void GetShowCmd(out int cmd);
    void SetShowCmd(int cmd);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int max, out int icon);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int icon);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, int reserved);
    void Resolve(IntPtr hwnd, int flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
interface IPropertyStore
{
    void GetCount(out uint count);
    void GetAt(uint index, out PropKey key);
    void GetValue(ref PropKey key, out PropVariant value);
    void SetValue(ref PropKey key, ref PropVariant value);
    void Commit();
}

[StructLayout(LayoutKind.Sequential, Pack = 4)]
struct PropKey
{
    public Guid Format;
    public uint Id;
    public PropKey(Guid format, uint id) { Format = format; Id = id; }
}

[StructLayout(LayoutKind.Sequential)]
struct PropVariant
{
    public ushort Type;
    public ushort Reserved1, Reserved2, Reserved3;
    public IntPtr Value;
    public IntPtr Extra;
}

static class Props
{
    static readonly Guid AppUserModel = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    public static readonly PropKey RelaunchCommand = new PropKey(AppUserModel, 2);
    public static readonly PropKey RelaunchIcon = new PropKey(AppUserModel, 3);
    public static readonly PropKey RelaunchName = new PropKey(AppUserModel, 4);
    public static readonly PropKey AppUserModelId = new PropKey(AppUserModel, 5);

    public static void Set(IPropertyStore store, PropKey key, string text)
    {
        var value = new PropVariant();
        value.Type = 31;                                  // VT_LPWSTR
        value.Value = Marshal.StringToCoTaskMemUni(text);
        try { store.SetValue(ref key, ref value); store.Commit(); }
        finally { Marshal.FreeCoTaskMem(value.Value); }
    }
}

/// <summary>Starts maxedcode's web server or finds the running one; stops it when the window closes.</summary>
static class Server
{
    static string Log, UrlFile;            // per port: a test window on another port must not mix them up
    public static Process Started;          // the server this window started (cmd.exe running python), or null

    /// <summary>The page URL with its token. Throws with a readable message when the server cannot start.</summary>
    public static string Ensure(int port)
    {
        Log = Path.Combine(MaxedcodeApp.Home, "server-" + port + ".log");
        UrlFile = Path.Combine(MaxedcodeApp.Home, "app-url-" + port + ".txt");
        if (Listening(port))
        {
            // A second window, or the server still finishing a turn after the last window closed: reuse it.
            // app-url.txt: the name before the URL file went per port
            foreach (string file in new[] { UrlFile, Path.Combine(MaxedcodeApp.Home, "app-url.txt") })
            {
                if (!File.Exists(file)) continue;
                string known = File.ReadAllText(file).Trim();
                if (known.Contains("127.0.0.1:" + port + "/") && TokenAccepted(known)) return known;
            }
            throw new Exception("Port " + port + " is taken by another program. Start maxedcode.ps1 -Port <other>.");
        }
        string agent = FindAgent();
        if (agent == null)
            throw new Exception("local_agent.py was not found near " + MaxedcodeApp.AppDir);
        File.Delete(Log);
        File.Delete(Log + ".err");
        // cmd.exe redirects the server's output into the log, where its first line is the URL.
        string command = "/d /s /c \"\"" + Python() + "\" \"" + agent + "\" --app --port " + port +
                         " 1>\"" + Log + "\" 2>\"" + Log + ".err\"\"";
        var info = new ProcessStartInfo("cmd.exe", command);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        Started = Process.Start(info);
        var deadline = DateTime.UtcNow.AddSeconds(30);
        while (DateTime.UtcNow < deadline)
        {
            Thread.Sleep(150);
            string text = ReadShared(Log);
            Match m = Regex.Match(text, @"maxedcode app: (http\S+)");
            if (m.Success)
            {
                File.WriteAllText(UrlFile, m.Groups[1].Value, Encoding.ASCII);
                return m.Groups[1].Value;
            }
            if (Started.HasExited) break;
        }
        Stop();
        string err = ReadShared(Log + ".err").Trim();
        if (err.Length > 1500) err = "..." + err.Substring(err.Length - 1500);
        throw new Exception("The maxedcode server did not start.\n\n" + (err.Length > 0 ? err : "See " + Log));
    }

    /// <summary>The window closed: the server exits once idle; if it cannot be told, the one we started is stopped.</summary>
    public static void Quit(string url)
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(url.Replace("/?t=", "/api/quit?t="));
            request.Method = "POST";
            request.ContentLength = 0;
            request.Timeout = 3000;
            request.Proxy = null;
            using (request.GetResponse()) { }
        }
        catch (Exception)
        {
            Stop();
        }
    }

    public static void Stop()
    {
        // The server runs maxedcode turns (python, PowerShell children): stop the whole tree.
        if (Started == null || Started.HasExited) return;
        var info = new ProcessStartInfo("taskkill.exe", "/PID " + Started.Id + " /T /F");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        using (var p = Process.Start(info)) p.WaitForExit(5000);
    }

    static string Python()
    {
        // The installer writes the Python it ran with into python.txt; otherwise "python" from PATH.
        try
        {
            string saved = File.ReadAllText(Path.Combine(MaxedcodeApp.AppDir, "python.txt")).Trim();
            if (File.Exists(saved)) return saved;
        }
        catch (IOException) { }
        return "python";
    }

    static string FindAgent()
    {
        // Work folder: desktop\app -> agent\local_agent.py; disk copy: ~\.maxedcode\desktop\app -> ~\.maxedcode.
        string dir = MaxedcodeApp.AppDir;
        for (int up = 0; up < 3 && dir != null; up++, dir = Path.GetDirectoryName(dir))
        {
            foreach (string candidate in new[] { Path.Combine(dir, "local_agent.py"),
                                                 Path.Combine(dir, "agent", "local_agent.py") })
                if (File.Exists(candidate)) return candidate;
        }
        return null;
    }

    static bool Listening(int port)
    {
        try
        {
            using (var client = new TcpClient())
            {
                IAsyncResult r = client.BeginConnect("127.0.0.1", port, null, null);
                bool ok = r.AsyncWaitHandle.WaitOne(500) && client.Connected;
                if (ok) client.EndConnect(r);
                return ok;
            }
        }
        catch (Exception) { return false; }
    }

    static bool TokenAccepted(string url)
    {
        // A valid token answers with a redirect that sets the cookie; a stale one gets 401.
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(url);
            request.AllowAutoRedirect = false;
            request.Timeout = 3000;
            request.Proxy = null;
            using (var response = (HttpWebResponse)request.GetResponse())
                return (int)response.StatusCode == 302;
        }
        catch (Exception) { return false; }
    }

    static string ReadShared(string path)
    {
        try
        {
            using (var f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            using (var r = new StreamReader(f, Encoding.UTF8))
                return r.ReadToEnd();
        }
        catch (IOException) { return ""; }
    }
}

/// <summary>The app window: the page in WebView2, links out to the default browser, size and theme remembered.</summary>
class MainWindow : Form
{
    static readonly string BoundsFile = Path.Combine(MaxedcodeApp.Home, "window.txt");
    static readonly Color Dark = Color.FromArgb(10, 10, 10), Light = Color.White;   // --bg of app.css
    readonly int port;
    readonly WebView2 web = new WebView2();
    readonly Label status = new Label();
    string url, origin;

    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    public MainWindow(int port)
    {
        this.port = port;
        Text = "maxedcode";
        Icon = new Icon(MaxedcodeApp.IconPath);
        MinimumSize = new Size(480, 360);
        LoadBounds();
        status.Text = "Starting maxedcode...";
        status.Dock = DockStyle.Fill;
        status.TextAlign = ContentAlignment.MiddleCenter;
        status.Font = new Font("Segoe UI", 11f);
        web.Dock = DockStyle.Fill;
        web.CreationProperties = new CoreWebView2CreationProperties();
        web.CreationProperties.UserDataFolder = Path.Combine(MaxedcodeApp.Home, "webview2");
        Controls.Add(web);
        Controls.Add(status);
        status.BringToFront();                              // covers the page until it has loaded
        SetTheme(true);                                     // the page's default; it reports the real one on load
        HandleCreated += delegate
        {
            MaxedcodeApp.TagWindow(Handle);                 // before the taskbar button exists
            MaxedcodeApp.SaveHandle(port, Handle);          // a second start focuses this window
        };
        Shown += delegate { Start(); };
    }

    async void Start()
    {
        Task<string> server = Task.Run(() => Server.Ensure(port));      // in parallel with the WebView2 start
        string error = null;
        bool noRuntime = false;
        try
        {
            CoreWebView2Environment.SetLoaderDllFolderPath(MaxedcodeApp.LibDir);
            await web.EnsureCoreWebView2Async(null);
        }
        catch (WebView2RuntimeNotFoundException) { noRuntime = true; }
        catch (Exception e) { error = e.Message; }
        try { url = await server; }
        catch (Exception e) { error = e.Message; }
        if (error != null)
        {
            status.Text = error;                            // visible in the window even if the box opens behind it
            Activate();
            MessageBox.Show(this, error, "maxedcode", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
            return;
        }
        if (noRuntime)
        {
            // No WebView2 on this PC (Windows 11 has it built in): the page in the default browser instead.
            MessageBox.Show(this, "Microsoft Edge WebView2 Runtime is not installed, so maxedcode opens in the browser.\n\n" +
                            "Install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703 to get the app window.",
                            "maxedcode", MessageBoxButtons.OK, MessageBoxIcon.Information);
            OpenOutside(url);
            url = null;                                     // a browser tab cannot be awaited: the server stays
            Close();
            return;
        }
        origin = new Uri(url).GetLeftPart(UriPartial.Authority);
        CoreWebView2 core = web.CoreWebView2;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.Settings.IsGeneralAutofillEnabled = false;
        core.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs e)
        {
            e.Handled = true;                               // links with target=_blank: the default browser
            OpenOutside(e.Uri);
        };
        core.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs e)
        {
            if (Ours(e.Uri)) return;                        // the app itself (the Browser tab has its own view)
            e.Cancel = true;
            OpenOutside(e.Uri);
        };
        core.NavigationCompleted += delegate(object s, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.IsSuccess) { status.Visible = false; web.Focus(); }
            else status.Text = "The maxedcode server does not answer (" + e.WebErrorStatus + ").\nClose the window and open it again.";
        };
        core.DocumentTitleChanged += delegate
        {
            string title = core.DocumentTitle;
            Text = string.IsNullOrWhiteSpace(title) || title.StartsWith("http") ? "maxedcode" : title;
        };
        core.WebMessageReceived += delegate(object s, CoreWebView2WebMessageReceivedEventArgs e)
        {
            object message;
            try { message = json.DeserializeObject(e.WebMessageAsJson); } catch (ArgumentException) { return; }
            var text = message as string;
            if (text == "theme:dark" || text == "theme:light") SetTheme(text == "theme:dark");
            var data = message as Dictionary<string, object>;
            if (data != null && data.ContainsKey("browser")) Browser(data);
            if (data != null && data.ContainsKey("notify"))
                Notify(Convert.ToString(data["title"]), data.ContainsKey("text") ? Convert.ToString(data["text"]) : "");
            if (data != null && data.ContainsKey("dropped")) Dropped(e, Convert.ToString(data["dropped"]));
        };
        core.ProcessFailed += delegate(object s, CoreWebView2ProcessFailedEventArgs e)
        {
            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited ||
                e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessUnresponsive)
                core.Reload();
        };
        core.Navigate(url);
    }

    // Files and folders dropped into the page: the page cannot see their paths, the window can (the page posts the
    // dropped File objects along with its message); the paths go back in the same order.
    void Dropped(CoreWebView2WebMessageReceivedEventArgs e, string token)
    {
        var paths = new List<string>();
        try
        {
            var extra = e.AdditionalObjects;
            if (extra != null)
                foreach (object o in extra)
                {
                    var f = o as CoreWebView2File;
                    paths.Add(f != null ? f.Path : "");
                }
        }
        catch (Exception) { }
        var reply = new Dictionary<string, object>();
        reply["dropped"] = token;
        reply["paths"] = paths;
        web.CoreWebView2.PostWebMessageAsJson(json.Serialize(reply));
    }

    // ── Notifications: a turn finished or asks for approval while the window is not in front ──
    NotifyIcon tray;

    [StructLayout(LayoutKind.Sequential)]
    struct FlashInfo { public uint Size; public IntPtr Hwnd; public uint Flags; public uint Count; public uint Timeout; }

    [DllImport("user32.dll")] static extern bool FlashWindowEx(ref FlashInfo info);

    void Notify(string title, string text)
    {
        if (ContainsFocus && WindowState != FormWindowState.Minimized) return;
        var flash = new FlashInfo();
        flash.Size = (uint)Marshal.SizeOf(typeof(FlashInfo));
        flash.Hwnd = Handle;
        flash.Flags = 3 | 12;                               // caption + taskbar, until the window comes to the front
        FlashWindowEx(ref flash);
        if (tray == null)
        {
            tray = new NotifyIcon();
            tray.Icon = Icon;
            tray.Text = "maxedcode";
            tray.BalloonTipClicked += delegate { BringBack(); };
            tray.Click += delegate { BringBack(); };
        }
        tray.Visible = true;
        tray.ShowBalloonTip(6000, string.IsNullOrWhiteSpace(title) ? "maxedcode" : title,
            string.IsNullOrWhiteSpace(text) ? " " : text, ToolTipIcon.None);
    }

    void BringBack()
    {
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
        if (tray != null) tray.Visible = false;
    }

    // ── Browser tabs of the right panel: a real browser (its own WebView2) laid over the tab's area of the page.
    // Any site opens (an iframe is refused by most), the page draws the address bar and sends the area's bounds.
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    readonly Dictionary<string, WebView2> views = new Dictionary<string, WebView2>();
    readonly Dictionary<string, string> pending = new Dictionary<string, string>();   // tab -> url before it is ready
    readonly HashSet<string> loading = new HashSet<string>();

    void Browser(Dictionary<string, object> data)
    {
        string op = Convert.ToString(data["browser"]), id = data.ContainsKey("id") ? Convert.ToString(data["id"]) : "";
        WebView2 view;
        views.TryGetValue(id, out view);
        if (op == "open") { Open(id, Convert.ToString(data["url"])); return; }
        if (view == null) return;
        CoreWebView2 c = view.CoreWebView2;
        if (op == "bounds")
        {
            bool visible = Convert.ToBoolean(data["visible"]);
            if (visible)
            {
                double scale = Convert.ToDouble(data["dpr"]);
                view.Bounds = new Rectangle((int)Math.Round(Convert.ToDouble(data["x"]) * scale),
                    (int)Math.Round(Convert.ToDouble(data["y"]) * scale), (int)Math.Round(Convert.ToDouble(data["w"]) * scale),
                    (int)Math.Round(Convert.ToDouble(data["h"]) * scale));
                view.BringToFront();
            }
            view.Visible = visible;
        }
        else if (op == "close")
        {
            views.Remove(id);
            Controls.Remove(view);
            view.Dispose();
        }
        else if (c == null) return;
        else if (op == "back" && c.CanGoBack) c.GoBack();
        else if (op == "forward" && c.CanGoForward) c.GoForward();
        else if (op == "reload") c.Reload();
        else if (op == "stop") c.Stop();
    }

    async void Open(string id, string address)
    {
        WebView2 view;
        if (views.TryGetValue(id, out view))
        {
            if (view.CoreWebView2 != null) view.CoreWebView2.Navigate(address);
            else pending[id] = address;
            return;
        }
        view = new WebView2();
        view.Visible = false;
        view.DefaultBackgroundColor = Color.White;
        views[id] = view;
        pending[id] = address;
        Controls.Add(view);
        view.BringToFront();
        try { await view.EnsureCoreWebView2Async(web.CoreWebView2.Environment); }
        catch (Exception) { return; }
        CoreWebView2 c = view.CoreWebView2;
        if (c == null) return;
        c.Settings.IsStatusBarEnabled = true;
        c.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs e)
        {
            e.Handled = true;                               // a link to a new window: in this tab
            c.Navigate(e.Uri);
        };
        c.NavigationStarting += delegate { loading.Add(id); Report(id, c, null); };
        c.NavigationCompleted += delegate(object s, CoreWebView2NavigationCompletedEventArgs e)
        {
            loading.Remove(id);
            Report(id, c, e.IsSuccess || e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled
                ? null : e.WebErrorStatus.ToString());
        };
        c.SourceChanged += delegate { Report(id, c, null); };
        c.HistoryChanged += delegate { Report(id, c, null); };
        c.DocumentTitleChanged += delegate { Report(id, c, null); };
        string first;
        if (pending.TryGetValue(id, out first)) { pending.Remove(id); c.Navigate(first); }
    }

    /// <summary>The tab's address, title and history state to the page (its address bar).</summary>
    void Report(string id, CoreWebView2 c, string error)
    {
        if (web.CoreWebView2 == null || !views.ContainsKey(id)) return;
        var state = new Dictionary<string, object>();
        state["browser"] = "state";
        state["id"] = id;
        state["url"] = c.Source;
        state["title"] = c.DocumentTitle;
        state["back"] = c.CanGoBack;
        state["forward"] = c.CanGoForward;
        state["loading"] = loading.Contains(id);
        state["error"] = error;
        web.CoreWebView2.PostWebMessageAsJson(json.Serialize(state));
    }

    bool Ours(string uri)
    {
        return uri.StartsWith(origin + "/", StringComparison.OrdinalIgnoreCase) || uri == origin ||
               uri.StartsWith("about:", StringComparison.OrdinalIgnoreCase);
    }

    static void OpenOutside(string uri)
    {
        if (!(uri.StartsWith("http://") || uri.StartsWith("https://") || uri.StartsWith("mailto:"))) return;
        try { Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true }); }
        catch (Exception) { }
    }

    void SetTheme(bool dark)
    {
        Color bg = dark ? Dark : Light;
        BackColor = bg;
        status.BackColor = bg;
        status.ForeColor = dark ? Color.FromArgb(160, 160, 160) : Color.FromArgb(90, 90, 90);
        web.DefaultBackgroundColor = bg;
        if (IsHandleCreated) DarkTitleBar(dark);
        else HandleCreated += delegate { DarkTitleBar(dark); };
    }

    void DarkTitleBar(bool dark)
    {
        int on = dark ? 1 : 0;
        DwmSetWindowAttribute(Handle, 20, ref on, 4);       // DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 2004+)
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        SaveBounds();
        base.OnFormClosing(e);
    }

    protected override void OnActivated(EventArgs e)
    {
        base.OnActivated(e);
        if (tray != null) tray.Visible = false;          // back in front: the notification icon goes
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        if (tray != null) tray.Dispose();
        base.OnFormClosed(e);
        if (url != null) Server.Quit(url);
    }

    // Window position: "x y width height maximized", restored only when it is still on a screen.
    void LoadBounds()
    {
        StartPosition = FormStartPosition.CenterScreen;
        Rectangle area = Screen.PrimaryScreen.WorkingArea;
        Size = new Size(Math.Min(area.Width * 4 / 5, 1800), Math.Min(area.Height * 17 / 20, 1150));
        try
        {
            string[] p = File.ReadAllText(BoundsFile).Split(' ');
            var r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
            foreach (Screen screen in Screen.AllScreens)
            {
                if (!screen.WorkingArea.IntersectsWith(r)) continue;
                StartPosition = FormStartPosition.Manual;
                Bounds = r;
                if (p.Length > 4 && p[4] == "1") WindowState = FormWindowState.Maximized;
                break;
            }
        }
        catch (Exception) { }
    }

    void SaveBounds()
    {
        Rectangle r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
        try
        {
            File.WriteAllText(BoundsFile, r.X + " " + r.Y + " " + r.Width + " " + r.Height + " " +
                                          (WindowState == FormWindowState.Maximized ? "1" : "0"));
        }
        catch (Exception) { }
    }
}
