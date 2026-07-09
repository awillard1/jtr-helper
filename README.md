# jtr-helper

John the Ripper wrapper and potfile merge utility.

## Overview

`jtr-helper` helps automate common John the Ripper workflows:

- Running cracking sessions across one or more hash files
- Managing session names and reruns
- Building/using wordlists
- Applying stacked rules
- Merging/processing potfile results

## Requirements

- Python 3.10+
- John the Ripper installed and available in your environment
- Hash files and wordlists organized locally (examples below)

## Quick Start

```bash
git clone https://github.com/awillard1/jtr-helper.git
cd jtr-helper
```

## Usage

### Example commands

```bash
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -f nt,raw-md5,raw-sha1 -w wordlists/master.lst -s -max 30
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w wordlists/master.lst
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w wordlists/master.lst -s
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w "wordlists/*" -r
python3.10 scripts/jtr-helper.py -b
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w wordlists/master.lst -c
```

### Notes

- If `-b` is used, the tool will build the master wordlist and exit (other parameters are ignored).
- If `-c` is used, stacked rules you select are applied.
- If no session is provided, a random session name is created automatically.

## Suggested Directory Layout

```text
.
├── scripts/
│   └── jtr-helper.py
├── hashes/
└── wordlists/
    └── master.lst
```

## Disclaimer

All information provided in this repository is for educational purposes only.

The authors are not responsible for misuse of the information.

Do not use these materials to gain unauthorized access and/or create malicious software.

Use this content only to build knowledge and support authorized security testing.
