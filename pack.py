import zipfile
import os

base_dir = r"d:\HuaweiMoveData\Users\HUAWEI\Desktop\ai_chat\robot\astrbot_plugin_zhixuewang"
output_zip = os.path.join(os.path.dirname(base_dir), "astrbot_plugin_zhixuewang.zip")
plugin_name = "astrbot_plugin_zhixuewang"

exclude_dirs = {".git", "__pycache__"}
exclude_files = {".gitignore", ".gitkeep", "astrbot_plugin_zhixuewang.zip", "pack.py", "geeked.py"}

with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
    info = zipfile.ZipInfo(plugin_name + "/")
    info.external_attr = 0o755 << 16
    zf.writestr(info, "")

    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        for f in sorted(files):
            if f in exclude_files or f.endswith(".pyc"):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, base_dir)
            rel_path = rel_path.replace("\\", "/")
            zf.write(full_path, f"{plugin_name}/{rel_path}")

    entries = zf.namelist()
    print(f"第1个条目: {entries[0]}")
    for n in entries[1:]:
        print(f"  {n}")

print(f"\n大小: {os.path.getsize(output_zip) / 1024:.1f} KB")