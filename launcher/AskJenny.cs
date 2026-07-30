using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("AskJenny")]
[assembly: AssemblyDescription("Ask Jenny local AI assistant and system tray launcher")]
[assembly: AssemblyCompany("Vasquez Integrators")]
[assembly: AssemblyProduct("AskJenny")]
[assembly: AssemblyCopyright("Copyright © 2026 Vasquez Integrators")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]
[assembly: AssemblyInformationalVersion("1.0.0")]

internal static class AskJennyLauncher
{
    [STAThread]
    private static void Main()
    {
        string applicationRoot = FindApplicationRoot();
        if (applicationRoot == null)
        {
            MessageBox.Show(
                "AskJenny could not find tray.ps1.\n\nKeep AskJenny.exe in the application folder, or set ASKJENNY_HOME to the application folder.",
                "AskJenny",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return;
        }

        string trayScript = Path.Combine(applicationRoot, "tray.ps1");
        ProcessStartInfo startInfo = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments =
                "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " +
                QuoteArgument(trayScript),
            WorkingDirectory = applicationRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        };

        try
        {
            Process.Start(startInfo);
        }
        catch (Exception exception)
        {
            MessageBox.Show(
                "AskJenny could not start the tray application.\n\n" + exception.Message,
                "AskJenny",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }

    private static string FindApplicationRoot()
    {
        string configuredRoot = Environment.GetEnvironmentVariable("ASKJENNY_HOME");
        if (ContainsTrayScript(configuredRoot))
        {
            return Path.GetFullPath(configuredRoot);
        }

        DirectoryInfo directory = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
        for (int depth = 0; depth < 4 && directory != null; depth++)
        {
            if (ContainsTrayScript(directory.FullName))
            {
                return directory.FullName;
            }
            directory = directory.Parent;
        }

        return null;
    }

    private static bool ContainsTrayScript(string directory)
    {
        return !string.IsNullOrWhiteSpace(directory)
            && File.Exists(Path.Combine(directory, "tray.ps1"));
    }

    private static string QuoteArgument(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}
