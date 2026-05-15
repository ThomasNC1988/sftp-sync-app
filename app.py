import os
import json
import stat
import paramiko
import subprocess
import threading
import shutil
from datetime import datetime, timedelta
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- Configuration ---
SFTP_HOST = os.getenv('SFTP_HOST', '192.168.1.72')
SFTP_PORT = int(os.getenv('SFTP_PORT', 22))
SFTP_USER = os.getenv('SFTP_USER', 'comma')
SFTP_KEY_PATH = os.getenv('SFTP_KEY_PATH', '/app/keys/comma_key.pem') 
REMOTE_DIR = os.getenv('REMOTE_DIR', '/data/media/0/realdata/')
LOCAL_DIR = os.getenv('LOCAL_DIR', '/app/downloads/')
TEMP_DIR = os.getenv('TEMP_DIR', '/app/temp/') # NEW: Added temp directory config
HISTORY_FILE = os.getenv('HISTORY_FILE', '/app/data/history.json')

os.makedirs(LOCAL_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True) # Ensure temp directory exists
os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)

sync_lock = threading.Lock()

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            try: return set(json.load(f))
            except: return set()
    return set()

def save_history(history):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(list(history), f)

def find_hevc_files(sftp, current_dir):
    target_files = []
    try:
        for item in sftp.listdir_attr(current_dir):
            item_path = f"{current_dir.rstrip('/')}/{item.filename}"
            if stat.S_ISDIR(item.st_mode):
                target_files.extend(find_hevc_files(sftp, item_path))
            elif stat.S_ISREG(item.st_mode) and item.filename.lower() == 'fcamera.hevc':
                target_files.append({
                    'remote_path': item_path,
                    'mtime': item.st_mtime
                })
    except Exception as e:
        print(f"Could not access {current_dir}: {e}")
    return target_files

def cleanup_old_files():
    print("Running cleanup: checking for files older than 30 days...")
    now = datetime.now()
    for filename in os.listdir(LOCAL_DIR):
        if filename.endswith('.mkv'):
            try:
                date_str = os.path.splitext(filename)[0]
                file_date = datetime.strptime(date_str, '%Y-%m-%d_%H-%M-%S')
                if (now - file_date) > timedelta(days=30):
                    os.remove(os.path.join(LOCAL_DIR, filename))
                    print(f"Deleted old drive: {filename}")
            except: continue

def sync_sftp_files():
    if not sync_lock.acquire(blocking=False):
        print("A sync check is already executing or waiting. Skipping this interval.")
        return

    try:
        print("Starting SFTP sync job...")
        history = load_history()
        
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, 
                key_filename=SFTP_KEY_PATH, timeout=10, banner_timeout=10
            )
            sftp = ssh.open_sftp()
            
            all_files = find_hevc_files(sftp, REMOTE_DIR)
            all_files.sort(key=lambda x: x['mtime'])
            
            drives = []
            if all_files:
                current_drive = [all_files[0]]
                for i in range(1, len(all_files)):
                    if all_files[i]['mtime'] - all_files[i-1]['mtime'] < 90:
                        current_drive.append(all_files[i])
                    else:
                        drives.append(current_drive)
                        current_drive = [all_files[i]]
                drives.append(current_drive)

            for drive in drives:
                new_segments = [seg for seg in drive if seg['remote_path'] not in history]
                
                if not new_segments:
                    continue
                
                drive_start_time = datetime.fromtimestamp(drive[0]['mtime']).strftime('%Y-%m-%d_%H-%M-%S')
                
                # CHANGED: Intermediate stitch files go to TEMP_DIR
                temp_mkv = os.path.join(TEMP_DIR, f"{drive_start_time}.mkv")
                concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
                
                # Final home for Plex to scan
                final_mkv = os.path.join(LOCAL_DIR, f"{drive_start_time}.mkv")
                
                print(f"Processing drive started at {drive_start_time} ({len(drive)} segments)...")
                
                temp_files = []
                try:
                    with open(concat_list_path, 'w') as f:
                        for i, segment in enumerate(drive):
                            # CHANGED: Downloads land in TEMP_DIR
                            temp_hevc = os.path.join(TEMP_DIR, f"temp_{i}.hevc")
                            print(f"  Downloading segment {i+1}/{len(drive)}...")
                            sftp.get(segment['remote_path'], temp_hevc)
                            f.write(f"file '{temp_hevc}'\n")
                            temp_files.append(temp_hevc)

                    print(f"  Stitching into single MKV in temp...")
                    subprocess.run([
                        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', 
                        '-i', concat_list_path, '-c', 'copy', temp_mkv
                    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    print(f"  Moving completed drive to Plex library...")
                    # Atomically move the finished file over to the real media directory
                    shutil.move(temp_mkv, final_mkv)

                    for segment in drive:
                        history.add(segment['remote_path'])
                    save_history(history)
                    print(f"  Successfully saved Drive: {drive_start_time}.mkv")

                except Exception as e:
                    print(f"  Failed to process drive {drive_start_time}: {e}")
                    if os.path.exists(temp_mkv): os.remove(temp_mkv)
                finally:
                    for tf in temp_files:
                        if os.path.exists(tf): os.remove(tf)
                    if os.path.exists(concat_list_path): os.remove(concat_list_path)

            sftp.close()
            ssh.close()
            cleanup_old_files()

        except Exception as e:
            print(f"SFTP Connection failed (Device likely offline): {e}")
    finally:
        sync_lock.release()

# --- Scheduler Setup ---
scheduler = BackgroundScheduler()
scheduler.add_job(func=sync_sftp_files, trigger="interval", minutes=10, misfire_grace_time=60, max_instances=1)
scheduler.start()

# --- Web Endpoints ---
@app.route('/')
def index(): return jsonify({"status": "running", "message": "SFTP Drive Stitcher active."})

@app.route('/history')
def get_history(): return jsonify({"downloaded_segments": list(load_history())})

@app.route('/trigger-sync')
def trigger_sync():
    threading.Thread(target=sync_sftp_files).start()
    return jsonify({"status": "Sync triggered manually (check logs)."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
