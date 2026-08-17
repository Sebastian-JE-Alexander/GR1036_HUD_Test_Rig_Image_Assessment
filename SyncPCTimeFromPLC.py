import time
import logging
from datetime import datetime, timezone, timedelta
from pylogix import PLC
import ctypes
from ctypes import wintypes

# ==================== CONFIGURATION ====================
PLC_IP = "192.168.10.3"
CHECK_INTERVAL = 300
RETRY_INTERVAL = 30
THRESHOLD_SECONDS = 10
LOG_FILE = "plc_time_sync.log"
# =======================================================

print("=" * 60)
print("PLC Time Sync Application has started...")
print(f"Target PLC IP      : {PLC_IP}")
print(f"Normal interval    : {CHECK_INTERVAL} seconds")
print(f"Retry on failure   : {RETRY_INTERVAL} seconds")
print("=" * 60)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", wintypes.WORD), ("wMonth", wintypes.WORD),
        ("wDayOfWeek", wintypes.WORD), ("wDay", wintypes.WORD),
        ("wHour", wintypes.WORD), ("wMinute", wintypes.WORD),
        ("wSecond", wintypes.WORD), ("wMilliseconds", wintypes.WORD),
    ]

def set_windows_time(dt):
    st = SYSTEMTIME()
    st.wYear = dt.year
    st.wMonth = dt.month
    st.wDay = dt.day
    st.wHour = dt.hour
    st.wMinute = dt.minute
    st.wSecond = dt.second
    st.wMilliseconds = dt.microsecond // 1000
    return bool(ctypes.windll.kernel32.SetSystemTime(ctypes.byref(st)))

def main():
    comm = None
    SAST = timezone(timedelta(hours=2))
    current_interval = CHECK_INTERVAL

    logging.info("PLC Time Sync started")

    while True:
        try:
            if comm is None:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Attempting to connect to PLC ({PLC_IP})...")
                logging.info(f"Attempting to connect to PLC at {PLC_IP}...")
                comm = PLC()
                comm.IPAddress = PLC_IP

            year     = comm.Read("g_DateTime.Year").Value
            month    = comm.Read("g_DateTime.Month").Value
            day      = comm.Read("g_DateTime.Day").Value
            hour     = comm.Read("g_DateTime.Hour").Value
            minute   = comm.Read("g_DateTime.Minute").Value
            second   = comm.Read("g_DateTime.Second").Value
            microsec = comm.Read("g_DateTime.MicroSecond").Value

            if None not in (year, month, day, hour, minute, second, microsec):
                if current_interval != CHECK_INTERVAL:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Connection restored to PLC.")
                    logging.info("Connection restored. Returning to normal interval.")
                    current_interval = CHECK_INTERVAL

                plc_local = datetime(year, month, day, hour, minute, second, microsec).replace(tzinfo=SAST)
                plc_utc = plc_local.astimezone(timezone.utc)
                pc_utc = datetime.now(timezone.utc)
                diff = abs((plc_utc - pc_utc).total_seconds())

                print(f"[{datetime.now().strftime('%H:%M:%S')}] PLC Local: {plc_local} | Diff: {diff:.1f}s")
                logging.info(f"PLC Local: {plc_local} | PC UTC: {pc_utc} | Diff: {diff:.1f}s")

                if diff > THRESHOLD_SECONDS:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Time difference is {diff:.1f}s — updating PC...")
                    if set_windows_time(plc_utc):
                        logging.info("PC system time updated successfully from PLC.")
                        print("    ✓ PC Time Updated Successfully.")

                        # Show both UTC and Local (SAST) time
                        new_time_utc = datetime.now(timezone.utc)
                        new_time_local = new_time_utc.astimezone(SAST)

                        print(f"    New PC Date/Time (UTC) : {new_time_utc}")
                        print(f"    New PC Date/Time (SAST): {new_time_local}")
                    else:
                        logging.error("Failed to set system time. Run as Administrator!")
                        print("    ✗ Failed to set system time. Run as Administrator!")
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Time is within tolerance. No update needed.")

            else:
                raise Exception("Received invalid/empty data from PLC")

        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✗ Connection/Read Error: {e}")
            logging.warning(f"Connection or read error: {e}. Retrying in {current_interval} seconds.")

            if comm:
                try:
                    comm.Close()
                except:
                    pass
                comm = None

            current_interval = RETRY_INTERVAL
            print(f"    → Will retry in {RETRY_INTERVAL} seconds...\n")

        time.sleep(current_interval)

if __name__ == "__main__":
    main()