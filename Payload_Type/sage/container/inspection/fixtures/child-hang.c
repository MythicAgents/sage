#include <windows.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    PROCESS_INFORMATION process;
    STARTUPINFOA startup;
    char command[2 * MAX_PATH + 16];
    char executable[MAX_PATH];
    if (argc > 1 && strcmp(argv[1], "child") == 0) {
        puts("fixture-child-started");
        fflush(stdout);
        Sleep(INFINITE);
        return 0;
    }
    ZeroMemory(&startup, sizeof(startup));
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    SetHandleInformation(startup.hStdOutput, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    SetHandleInformation(startup.hStdError, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    ZeroMemory(&process, sizeof(process));
    if (!GetModuleFileNameA(NULL, executable, sizeof(executable)))
        return 2;
    _snprintf(command, sizeof(command), "\"%s\" child", executable);
    if (!CreateProcessA(executable, command, NULL, NULL, TRUE, 0, NULL, NULL, &startup, &process))
        return 2;
    puts("fixture-child-created");
    fflush(stdout);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    Sleep(INFINITE);
    return 0;
}
