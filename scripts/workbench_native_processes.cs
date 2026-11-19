using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Zhimeng.Workbench {
    // Local Windows queries avoid WMI/CIM. Query errors must not look like
    // missing processes or unused ports to the start/stop identity checks.
    public static class NativeProcesses {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, int processId);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        [DllImport("ntdll.dll")]
        private static extern int NtQueryInformationProcess(IntPtr process, int infoClass,
            IntPtr information, int length, out int requiredLength);
        [DllImport("iphlpapi.dll")]
        private static extern uint GetExtendedTcpTable(IntPtr table, ref int size,
            bool ordered, int family, int tableClass, uint reserved);

        public static string CommandLine(int processId) {
            IntPtr process = OpenProcess(0x1000, false, processId);
            if (process == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            try {
                int size;
                NtQueryInformationProcess(process, 60, IntPtr.Zero, 0, out size);
                if (size < IntPtr.Size * 2 || size > 1024 * 1024)
                    throw new InvalidOperationException("无法取得进程命令行长度。");
                IntPtr buffer = Marshal.AllocHGlobal(size);
                try {
                    int returned;
                    int status = NtQueryInformationProcess(process, 60, buffer, size, out returned);
                    if (status != 0) throw new InvalidOperationException(
                        "无法读取进程命令行，NTSTATUS=0x" + status.ToString("X8"));
                    int length = (ushort)Marshal.ReadInt16(buffer);
                    IntPtr text = Marshal.ReadIntPtr(buffer, IntPtr.Size == 8 ? 8 : 4);
                    long offset = text.ToInt64() - buffer.ToInt64();
                    if ((length & 1) != 0 || offset < 0 || offset + length > size)
                        throw new InvalidOperationException("进程命令行数据不完整。");
                    return Marshal.PtrToStringUni(text, length / 2) ?? "";
                } finally { Marshal.FreeHGlobal(buffer); }
            } finally { CloseHandle(process); }
        }

        public static int[] ListenerPids(int port) {
            if (port < 1 || port > 65535) throw new ArgumentOutOfRangeException("port");
            var owners = new HashSet<int>();
            foreach (int family in new[] { 2, 23 }) {
                int size = 0;
                uint error = GetExtendedTcpTable(IntPtr.Zero, ref size, false, family, 3, 0);
                if (error != 0 && error != 122) throw new Win32Exception((int)error);
                if (size == 0) continue;
                for (int attempt = 0; ; attempt++) {
                    int allocated = size;
                    IntPtr buffer = Marshal.AllocHGlobal(allocated);
                    try {
                        error = GetExtendedTcpTable(buffer, ref size, false, family, 3, 0);
                        if (error == 122 && attempt < 3) continue;
                        if (error != 0) throw new Win32Exception((int)error);
                        int count = Marshal.ReadInt32(buffer);
                        int rowSize = family == 2 ? 24 : 56;
                        int portOffset = family == 2 ? 8 : 20;
                        int pidOffset = family == 2 ? 20 : 52;
                        if (count < 0 || count > (allocated - 4) / rowSize)
                            throw new InvalidOperationException("TCP 监听表数据不完整。");
                        for (int row = 0; row < count; row++) {
                            IntPtr address = IntPtr.Add(buffer, 4 + row * rowSize);
                            uint networkPort = (uint)Marshal.ReadInt32(address, portOffset);
                            int localPort = (int)(((networkPort & 255) << 8) | ((networkPort >> 8) & 255));
                            if (localPort == port) owners.Add(Marshal.ReadInt32(address, pidOffset));
                        }
                        break;
                    } finally { Marshal.FreeHGlobal(buffer); }
                }
            }
            int[] result = new int[owners.Count];
            owners.CopyTo(result);
            Array.Sort(result);
            return result;
        }
    }
}
