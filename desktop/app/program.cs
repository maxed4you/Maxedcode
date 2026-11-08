// maxedcode app as its own exe (build-exe.ps1 compiles it with maxedcode.cs): the same window maxedcode.ps1 opens.
//   maxedcode.exe [--port N]   the window          maxedcode.exe --shortcuts   desktop / Start menu shortcuts
//   maxedcode.exe --check      load WebView2 and exit 0 (the installer's test that Windows lets the exe run)
using System;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Windows.Forms;

static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        string here = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        // the WebView2 assemblies stay in lib\, as for maxedcode.ps1
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object s, ResolveEventArgs e)
        {
            string file = Path.Combine(here, "lib", new AssemblyName(e.Name).Name + ".dll");
            return File.Exists(file) ? Assembly.LoadFrom(file) : null;
        };
        try { return Run(here, args); }
        catch (Exception error)
        {
            if (Array.IndexOf(args, "--check") < 0)
                MessageBox.Show("maxedcode app did not start:\n\n" + error.Message, "maxedcode", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]          // WebView2 types load only after AssemblyResolve is set
    static int Run(string here, string[] args)
    {
        if (Array.IndexOf(args, "--check") >= 0)
        {
            GC.KeepAlive(typeof(Microsoft.Web.WebView2.WinForms.WebView2).Assembly);
            return 0;
        }
        MaxedcodeApp.UseExe = true;
        if (Array.IndexOf(args, "--shortcuts") >= 0)
        {
            foreach (string made in MaxedcodeApp.Shortcuts(here)) Console.WriteLine("Shortcut: " + made);
            return 0;
        }
        int port = 3077, i = Array.IndexOf(args, "--port");
        if (i >= 0 && i + 1 < args.Length) int.TryParse(args[i + 1], out port);
        MaxedcodeApp.Start(here, port);
        return 0;
    }
}
