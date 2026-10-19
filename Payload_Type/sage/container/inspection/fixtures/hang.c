#include <stdio.h>
#include <windows.h>

int main(void) {
    puts("fixture-hang-started");
    fflush(stdout);
    Sleep(INFINITE);
    return 0;
}
