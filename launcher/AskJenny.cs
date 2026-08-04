using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("AskJenny")]
[assembly: AssemblyDescription("Ask Jenny local AI assistant and system tray launcher")]
[assembly: AssemblyCompany("Vasquez Integrators")]
[assembly: AssemblyProduct("AskJenny")]
[assembly: AssemblyCopyright("Copyright © 2026 Vasquez Integrators")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]
[assembly: AssemblyInformationalVersion("1.0.0")]

/// <summary>
/// Minimal Windows bootstrapper that starts the PowerShell tray host or reopens
/// the browser when that host is already running.
/// </summary>
internal static class AskJennyLauncher
{
    private const string ApplicationUrl = "http://127.0.0.1:8000";
    private const string BrowserSessionStatusUrl =
        ApplicationUrl + "/api/ui-sessions/active";
    private const string TrayMutexName = @"Local\AskJennyServerTray";

    [STAThread]
    private static void Main()
    {
        // A second launch behaves like "open app" instead of creating another tray.
        if (IsTrayRunning())
        {
            if (!HasActiveBrowserSession())
            {
                OpenApplicationInBrowser();
            }
            return;
        }

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

    private static bool IsTrayRunning()
    {
        // The tray process owns this named mutex for its entire lifetime.
        try
        {
            using (Mutex existingMutex = Mutex.OpenExisting(TrayMutexName))
            {
                return true;
            }
        }
        catch (WaitHandleCannotBeOpenedException)
        {
            return false;
        }
        catch (UnauthorizedAccessException)
        {
            return true;
        }
    }

    private static bool HasActiveBrowserSession()
    {
        // Failure means "unknown/inactive" so an explicit user launch still opens a tab.
        try
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(
                BrowserSessionStatusUrl
            );
            request.Method = "GET";
            request.Timeout = 2500;
            request.ReadWriteTimeout = 2500;
            request.CachePolicy = new System.Net.Cache.RequestCachePolicy(
                System.Net.Cache.RequestCacheLevel.NoCacheNoStore
            );

            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            using (Stream responseStream = response.GetResponseStream())
            using (StreamReader reader = new StreamReader(responseStream))
            {
                string payload = reader.ReadToEnd();
                return Regex.IsMatch(
                    payload,
                    "\"active\"\\s*:\\s*true",
                    RegexOptions.IgnoreCase
                );
            }
        }
        catch
        {
            return false;
        }
    }

    private static void OpenApplicationInBrowser()
    {
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = ApplicationUrl,
                UseShellExecute = true
            });
        }
        catch (Exception exception)
        {
            MessageBox.Show(
                "AskJenny could not open the application in your browser.\n\n" +
                exception.Message,
                "AskJenny",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }

    private static string FindApplicationRoot()
    {
        // Support installed layouts via an override, then portable layouts near the EXE.
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
