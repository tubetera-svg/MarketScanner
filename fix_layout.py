import re

with open(r'D:\Sid\MarketScanner\frontend\app\page.tsx', 'r', encoding='utf-8') as f:
    content = f.read()

# The current file has duplicate workspace blocks. We need to extract unique sections.
# Strategy: take everything before the first <div className="workspace">
# then build the correct structure

# Find the first workspace div
first_workspace = content.find('      <div className="workspace">')

# Find the footer
footer_start = content.rfind('      <footer>')

before = content[:first_workspace]

# Extract aside from first workspace
aside_match = re.search(r'(<aside className="controls panel">.*?</aside>)', content, re.DOTALL)
aside = aside_match.group(1) if aside_match else ''

# Extract scan-controls
scan_match = re.search(r'(<section className="scan-controls">.*?</section>\s*</section>)', content, re.DOTALL)
scan_controls = scan_match.group(1) if scan_match else ''

# Extract strategy-panel
strategy_match = re.search(r'(<section className="panel strategy-panel">.*?</section>)', content, re.DOTALL)
strategy_panel = strategy_match.group(1) if strategy_match else ''

# Extract results
results_match = re.search(r'(<section className="results">.*?</section>)', content, re.DOTALL)
results = results_match.group(1) if results_match else ''

footer = content[footer_start:]

new_workspace = f'''      <div className="workspace">
        {aside}
        <main className="main-content">
{scan_controls}
{strategy_panel}
{results}
        </main>
      </div>
'''

new_content = before + new_workspace + footer

with open(r'D:\Sid\MarketScanner\frontend\app\page.tsx', 'w', encoding='utf-8') as f:
    f.write(new_content)

print('Done')
