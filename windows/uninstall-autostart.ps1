# ============================================================
# upsbridge Windows 开机自启卸载（只删计划任务与防火墙规则，不动配置和日志）
# 以管理员身份运行
# ============================================================
schtasks /End /TN "upsbridge" >$null 2>$null
schtasks /Delete /F /TN "upsbridge" >$null 2>$null
netsh advfirewall firewall delete rule name="upsbridge" >$null 2>$null

Write-Host "已卸载开机自启（计划任务 + 防火墙规则）。"
Write-Host ("配置与日志仍保留在 " + $env:ProgramData + "\upsbridge ，要彻底移除请手动删除该目录。")
