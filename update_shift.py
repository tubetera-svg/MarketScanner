with open(r('D:\\Sid\\MarketScanner\\update_shift.py', 'w') { f.write(''with open(r('D:\\Sid\\MarketScanner\\frontend\\app\\page.tsx', 'r', encoding='utf-8') as f:
    content = f.read()

if old_code in content:
    content = content.replace(old_code, new_code)
    with open(r'"$D:\\Sid\\MarketScanner\\frontend\\app\\page.tsx', 'w', encoding='utf-8') as f:
      f.write(content)
    print('Successfully updated shiftStrategyDate function')
else:
    print('Could not find the target code block')
')