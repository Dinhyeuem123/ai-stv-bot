import re, sys
sys.stdout.reconfigure(encoding='utf-8')
c = open('vm_discord_bot.py','r',encoding='utf-8').read()
lines = c.split('\n')

t_calls_found = re.findall(r"_t\(lang,\s*\"([^\"]*)\",\s*\"([^\"]*)\"\)", c)
print(f"Found {len(t_calls_found)} _t() calls")

# Check for emoji used in embed titles (standalone emoji without _t)
for i, line in enumerate(lines, 1):
    s = line.strip()
    if '\u200b' in s:
        print(f"Line {i}: contains zero-width space")
