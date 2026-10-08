#include <stdio.h>

int main(void) {
    unsigned int i;
    for (i = 0; i < 262144; ++i) {
        fputc('O', stdout);
        fputc('E', stderr);
    }
    return 0;
}
