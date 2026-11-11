/* MUSICXX.app's executable. It runs the app's bundled Python inside this process, so macOS sees "MUSICXX"
 * (Dock name, icon, and the Desktop / microphone permission prompts). With no arguments it starts MUSICXX;
 * with arguments it behaves exactly like `python`, which is how MUSICXX runs its tools (sys.executable). */
#include <Python.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char exe[PATH_MAX], contents[PATH_MAX], home[PATH_MAX], script[PATH_MAX], userbase[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, contents)) return 1;
    *strrchr(contents, '/') = 0;  /* .../Contents/MacOS */
    *strrchr(contents, '/') = 0;  /* .../Contents */
    const char *h = getenv("HOME") ? getenv("HOME") : "";
    snprintf(home, sizeof home, "%s/Resources/python", contents);
    /* self-updates land in ~/.musicxx/bundle-app: macOS may stop an app rewriting its own bundle */
    snprintf(script, sizeof script, "%s/.musicxx/bundle-app/musicxx.py", h);
    if (access(script, R_OK) != 0) snprintf(script, sizeof script, "%s/Resources/app/musicxx.py", contents);
    /* daily tool upgrades go here too (pip --user), for the same reason */
    snprintf(userbase, sizeof userbase, "%s/.musicxx/bundle-packages", h);
    setenv("PYTHONUSERBASE", userbase, 0);

    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    PyStatus st = PyConfig_SetBytesString(&config, &config.home, home);  /* honoured even with -I */
    if (!PyStatus_Exception(st)) {
        char *start[] = {argv[0], script};
        st = argc > 1 ? PyConfig_SetBytesArgv(&config, argc, argv) : PyConfig_SetBytesArgv(&config, 2, start);
    }
    if (!PyStatus_Exception(st)) st = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(st)) Py_ExitStatusException(st);
    return Py_RunMain();
}
