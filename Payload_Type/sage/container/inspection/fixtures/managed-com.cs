using System;
using System.Runtime.InteropServices;

internal static class ManagedComFixture
{
    [DllImport("ole32.dll")]
    private static extern int CoInitializeEx(IntPtr reserved, uint coInit);

    [DllImport("ole32.dll")]
    private static extern int CoInitializeSecurity(
        IntPtr securityDescriptor, int authenticationServices, IntPtr services,
        IntPtr reserved, uint authenticationLevel, uint impersonationLevel,
        IntPtr authenticationList, uint capabilities, IntPtr reserved2);

    [DllImport("ole32.dll")]
    private static extern void CoUninitialize();

    public static int Main(string[] args)
    {
        int initialize = CoInitializeEx(IntPtr.Zero, 0);
        int security = CoInitializeSecurity(
            IntPtr.Zero, -1, IntPtr.Zero, IntPtr.Zero, 6, 3,
            IntPtr.Zero, 0, IntPtr.Zero);
        Console.WriteLine("fixture-managed-com initialize=0x{0:x8} security=0x{1:x8}",
            initialize, security);
        if (initialize >= 0)
            CoUninitialize();
        return 0;
    }
}
