import subprocess

def apply_hook(hook_name: str, manifest: dict):
    command = "./hooks/" + hook_name + " --plugin " + manifest.get("name", "")
    return subprocess.run(command, shell=True, capture_output=True, text=True)
