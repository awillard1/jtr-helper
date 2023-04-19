# jtr-helper
John the Ripper wrapper and potfile merge

example usage:

```
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w wordlists/master.lst
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w wordlists/master.lst -s
python3.10 scripts/jtr-helper.py -hash 'hashes/*' -session hacktastic -min 8 -max 28 -f nt -w "wordlists/*" -r
python3.10 scripts/jtr-helper.py -b
```

If -b is used, even if there are other parameters, it will just build the master wordlist and exit.


All the information provided on this site is for educational purposes only.

The site or the authors are not responsible for any misuse of the information.

You shall not misuse the information to gain unauthorized access and/or write malicious programs.

These information shall only be used to expand knowledge and not for causing malicious or damaging attacks.
