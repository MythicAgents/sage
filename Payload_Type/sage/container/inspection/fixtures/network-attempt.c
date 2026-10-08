#include <stdio.h>
#include <winsock2.h>
#include <ws2tcpip.h>

int main(void) {
    WSADATA data;
    struct sockaddr_in address;
    SOCKET socket_handle;
    int result;
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0)
        return 2;
    socket_handle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    address.sin_family = AF_INET;
    address.sin_port = htons(9);
    InetPtonA(AF_INET, "192.0.2.1", &address.sin_addr);
    result = connect(socket_handle, (struct sockaddr *)&address, sizeof(address));
    printf("fixture-network connect=%d error=%d\n", result, WSAGetLastError());
    closesocket(socket_handle);
    WSACleanup();
    return result == SOCKET_ERROR ? 0 : 3;
}
