import os
import sys
import subprocess
import platform
import venv
import time
from pathlib import Path

# --- Configuration ---
SOURCE_DIR_NAME = "ToxTarget_Source"
MAIN_SCRIPT_NAME = "main.py"
VENV_DIR_NAME = "venv"
REQ_FILE_NAME = "requirements.txt"

# List of PyPI mirrors to try in order.
# 1. Default (Official PyPI) - Best for global users.
# 2. Tsinghua University - Stable and fast for China.
# 3. Aliyun - Reliable alternative for China.
# 4. Tencent Cloud - Another backup.
PIP_MIRRORS = [
    None,  # Default official source
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
]

def get_paths():
    """Resolve absolute paths for the application."""
    base_dir = Path(__file__).parent.absolute()
    source_dir = base_dir / SOURCE_DIR_NAME
    main_script = source_dir / MAIN_SCRIPT_NAME
    req_file = source_dir / REQ_FILE_NAME
    venv_dir = base_dir / VENV_DIR_NAME
    
    return base_dir, source_dir, main_script, req_file, venv_dir

def get_venv_executable(venv_dir):
    """Return paths to python and pip executables inside the virtual environment."""
    if platform.system() == "Windows":
        python_exe = venv_dir / "Scripts" / "python.exe"
        pip_exe = venv_dir / "Scripts" / "pip.exe"
    else:
        # Linux / macOS
        python_exe = venv_dir / "bin" / "python"
        pip_exe = venv_dir / "bin" / "pip"
    
    return str(python_exe), str(pip_exe)

def create_venv_if_missing(venv_dir):
    """Check if venv exists, create it if not."""
    if not venv_dir.exists():
        print(f"[INIT] Creating virtual environment in: {venv_dir} ...")
        print("       This may take a few seconds...")
        try:
            builder = venv.EnvBuilder(with_pip=True)
            builder.create(venv_dir)
            print("[SUCCESS] Virtual environment created.")
        except Exception as e:
            print(f"[ERROR] Failed to create virtual environment: {e}")
            input("Press Enter to exit...")
            sys.exit(1)
    else:
        print("[CHECK] Virtual environment exists.")

def upgrade_pip(python_exe):
    """Attempt to upgrade pip to the latest version."""
    try:
        subprocess.run(
            [python_exe, "-m", "pip", "install", "--upgrade", "pip"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass # Non-critical failure

def install_requirements(pip_exe, req_file):
    """
    Install dependencies with a robust retry mechanism using different mirrors.
    """
    if not req_file.exists():
        print(f"[WARNING] Requirements file not found: {req_file}")
        print("          Attempting to run without installing dependencies.")
        return

    print("[INSTALL] Checking and installing dependencies...")
    
    success = False
    
    for mirror in PIP_MIRRORS:
        mirror_name = mirror if mirror else "Official PyPI"
        print(f"   -> Trying source: {mirror_name} ...")
        
        cmd = [pip_exe, "install", "-r", str(req_file), "--disable-pip-version-check"]
        if mirror:
            cmd.extend(["-i", mirror])
            # Set shorter timeout for mirrors to fail fast if unreachable
            cmd.extend(["--timeout", "30"]) 

        try:
            subprocess.check_call(cmd, stdout=sys.stdout, stderr=sys.stderr)
            success = True
            print(f"[SUCCESS] Dependencies installed successfully using {mirror_name}.")
            break  # Exit loop on success
        except subprocess.CalledProcessError:
            print(f"[WARNING] Failed to install using {mirror_name}. Switching to next source...")
            time.sleep(1) # Brief pause before retry

    if not success:
        print("\n[CRITICAL ERROR] Failed to install dependencies from all sources.")
        print("Please check your internet connection or configure a proxy.")
        input("Press Enter to exit...")
        sys.exit(1)

def run_main_app(python_exe, main_script, source_dir):
    """Launch the main application."""
    if not main_script.exists():
        print(f"[ERROR] Main script not found: {main_script}")
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"\n[LAUNCH] Starting ToxTarget-AI ({MAIN_SCRIPT_NAME})...\n")
    print("-" * 60)
    
    try:
        # 'cwd' is set to source_dir so the app can find local assets like .gct/.tsv files
        subprocess.run([python_exe, str(main_script)], cwd=str(source_dir), check=True)
    except subprocess.CalledProcessError as e:
        print(f"\n[CRASH] Application exited with error code: {e.returncode}")
        input("Press Enter to view logs, then exit...")
    except KeyboardInterrupt:
        print("\n[STOP] Application stopped by user.")

def main():
    # Enable ANSI colors in Windows terminal if possible
    os.system("") 
    
    print("==================================================")
    print("      ToxTarget-AI Auto-Launcher & Installer")
    print("==================================================\n")

    base_dir, source_dir, main_script, req_file, venv_dir = get_paths()
    
    # 1. Setup Virtual Environment
    create_venv_if_missing(venv_dir)
    
    # Get paths to the isolated python/pip
    python_exe, pip_exe = get_venv_executable(venv_dir)
    
    # Optional: Upgrade pip silently to avoid warnings
    upgrade_pip(python_exe)
    
    # 2. Install Dependencies (with Mirror Fallback)
    install_requirements(pip_exe, req_file)
    
    # 3. Launch App
    run_main_app(python_exe, main_script, source_dir)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[FATAL ERROR] Launcher crashed: {e}")
        import traceback
        traceback.print_exc()
        input("Press Enter to exit...")


