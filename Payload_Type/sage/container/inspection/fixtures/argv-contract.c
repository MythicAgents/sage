#include <windows.h>
#include <shellapi.h>
#include <stdio.h>

/* Inert fixed vectors: exercise the API used by Apollo's assembly runners.
 * This fixture never loads a supplied executable or dispatches a command. */
static void json_string(const wchar_t *value) {
    putchar('"');
    while (*value) {
        unsigned int c = (unsigned short)*value++;
        if (c == '"' || c == '\\') {
            putchar('\\');
            putchar((int)c);
        } else if (c < 32 || c > 126) {
            printf("\\u%04x", c);
        } else {
            putchar((int)c);
        }
    }
    putchar('"');
}

int main(void) {
    const wchar_t *vectors[] = {
        L"request --ca \"server\\CA Name\" --template User",
        L"probe --path \"C:\\Program Files\\Tool\\\\\"",
        L"probe --subject \"CN=Alice \\\"QA\\\"\"",
        L"probe --text \"\" --literal \"&|;$(echo nope)\"",
        L"probe --list one \"two words\" three",
        L"probe --unicode \"\x03b1 \x03b2\"",
        L"probe --path C:\\plain\\file --number 2048"
    };
    size_t n;
    for (n = 0; n < sizeof(vectors) / sizeof(vectors[0]); n++) {
        int count = 0, i;
        LPWSTR *args = CommandLineToArgvW(vectors[n], &count);
        if (!args) return 2;
        printf("{\"command\":");
        json_string(vectors[n]);
        printf(",\"argv\":[");
        for (i = 0; i < count; i++) {
            if (i) putchar(',');
            json_string(args[i]);
        }
        puts("]}");
        LocalFree(args);
    }
    return 0;
}
