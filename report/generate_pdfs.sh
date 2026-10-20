pandoc report.md -o report.pdf
PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium MERMAID_FILTER_SCALE=4 pandoc presentation.md --filter mermaid-filter -o presentation.pdf
rm mermaid-filter.err
