// maxedcode setup (installer\build.ps1 compiles it into dist\Maxedcode_setup.exe). It replaced install.bat, which
// some PCs' antivirus or policy stopped (a .bat that decodes a packed payload looks suspicious).
//   1. the program (app.zip, an embedded resource) is unpacked into %TEMP%
//   2. bootstrap.ps1 finds Python 3.10+ (not the Microsoft Store one) or installs Python 3.12 for this user
//   3. install.py opens the setup window: it installs the program, downloads the Python packages, WebView2...
// Arguments go to install.py as they are: Maxedcode_setup.exe --yes --base-url 192.168.0.10:1234 --model qwen3
// The log of every run: %TEMP%\maxedcode-setup.log
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;

static class Setup
{
    static string work;

    static int Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        Console.Title = "maxedcode setup";
        Console.WriteLine();
        Console.WriteLine("  maxedcode setup");
        Console.WriteLine("  ---------------");
        Console.WriteLine();
        try { return Run(args); }
        catch (Exception error)
        {
            Fail("Setup stopped: " + error.Message);
            return 1;
        }
        finally { Cleanup(); }
    }

    static int Run(string[] args)
    {
        work = Path.Combine(Path.GetTempPath(), "maxedcode-setup-" + Guid.NewGuid().ToString("N").Substring(0, 10));
        Console.WriteLine("  unpacking ...");
        using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("app.zip"))
        {
            if (payload == null) { Fail("The program is missing from this setup file. Download it again."); return 1; }
            Directory.CreateDirectory(work);
            string zip = Path.Combine(work, "app.zip");
            using (var f = File.Create(zip)) payload.CopyTo(f);
            ZipFile.ExtractToDirectory(zip, work);
            File.Delete(zip);
        }
        string app = Path.Combine(work, "app");
        if (!File.Exists(Path.Combine(app, "install.py")) || !File.Exists(Path.Combine(app, "bootstrap.ps1")))
        {
            Fail("The program inside the setup could not be unpacked (an antivirus may have removed part of it).");
            return 1;
        }

        // Python: bootstrap.ps1 writes the path of a good python.exe into a file (no quoting troubles)
        string pyFile = Path.Combine(Path.GetTempPath(), "maxedcode-python-" + Guid.NewGuid().ToString("N").Substring(0, 10) + ".txt");
        var boot = new ProcessStartInfo("powershell.exe",
            "-NoProfile -ExecutionPolicy Bypass -Command \"& ([scriptblock]::Create([IO.File]::ReadAllText($env:MX_BOOT)))\"");
        boot.UseShellExecute = false;
        boot.EnvironmentVariables["MX_BOOT"] = Path.Combine(app, "bootstrap.ps1");
        boot.EnvironmentVariables["MX_PYFILE"] = pyFile;
        int rc = RunWait(boot);
        string py = File.Exists(pyFile) ? File.ReadAllText(pyFile).Trim() : "";
        try { File.Delete(pyFile); } catch (IOException) { }
        if (rc != 0 || py == "" || !File.Exists(py))
        {
            Fail("Could not find or install Python 3.10+.\n  Install Python 3.12 from https://www.python.org/downloads/ and tick\n" +
                 "  \"Add python.exe to PATH\", then run this setup again.");
            return 1;
        }

        var install = new ProcessStartInfo(py, Quote(Path.Combine(app, "install.py")) + Join(args));
        install.UseShellExecute = false;
        install.WorkingDirectory = app;
        install.EnvironmentVariables["MX_SETUP_BAT"] = "1";      // the setup window minimizes this console
        rc = RunWait(install);
        if (rc != 0) Fail("Setup did not finish.");
        return rc;
    }

    static int RunWait(ProcessStartInfo info)
    {
        using (var p = Process.Start(info))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }

    static void Fail(string text)
    {
        Console.WriteLine();
        Console.WriteLine("  [ERROR] " + text);
        Console.WriteLine("  The log: " + Path.Combine(Path.GetTempPath(), "maxedcode-setup.log"));
        Console.WriteLine();
        Console.WriteLine("  Press any key to close.");
        try { Console.ReadKey(true); } catch (InvalidOperationException) { }
    }

    static void Cleanup()
    {
        if (work == null || !Directory.Exists(work)) return;
        try { Directory.Delete(work, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    static string Join(string[] args)
    {
        var sb = new StringBuilder();
        foreach (string a in args) sb.Append(' ').Append(Quote(a));
        return sb.ToString();
    }

    // Windows command-line quoting (CommandLineToArgvW rules): what Python gets back in sys.argv
    static string Quote(string a)
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return a;
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in a)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') sb.Append('\\', slashes * 2 + 1).Append('"');
            else sb.Append('\\', slashes).Append(c);
            slashes = 0;
        }
        return sb.Append('\\', slashes * 2).Append('"').ToString();
    }
}
