import os
import re
import argparse
import subprocess
import signal
import potpy
import random
import string

######## BEGIN CONFIGURATION ########
johnConf = "/home/willard/src/john/run/john.conf"
johnLocalConf = "/home/willard/src/john/run/john-local.conf"
jtrLocation = "/home/willard/src/john/run/john"
######## END CONFIGURATION   ######## 

def setJohnFork():
    global johnFork
    johnFork = input("Enter the --fork value for John (1 to " + str(os.cpu_count()) + "): ")    

    if (johnFork.isnumeric() == False):
        print("--fork was not numeric. Please set --fork to a value between 1 and " + str(os.cpu_count()))
        exit()
    elif (int(johnFork) < 1 or int(johnFork) > os.cpu_count()):
        print("Please set --fork to a value between 1 and " + str(os.cpu_count()))
        exit()

def readConf():
    i = 0
    if (os.path.exists(johnConf) == False):
        print("Unable to locate john.conf: " + johnConf + "\r\nExiting")
        exit()
    jtrconf = open(johnConf, "r")
    for line in jtrconf:
        match = re.match(r"^\[List.Rules.(.*)\].*$", line)
        if match:
            rule = match.group(1)
            print("["+str(i)+"] "+rule)
            ruleList.append(rule)
            i = i + 1
    if (os.path.exists(johnLocalConf)):
        jtrlocalconf = open(johnLocalConf,"r")
        for line in jtrlocalconf:
            match = re.match(r"^\[List.Rules.(.*)\].*$", line)
            if match:
                rule = match.group(1)
                print("["+str(i)+"] "+rule)
                ruleList.append(rule)
                i = i + 1
    #AddKorelogic
    print("["+str(i)+"] korelogic")
    ruleList.append("korelogic")

def setSessionIfNull():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
    
def loopCrack(rule, crule):
    wordlistdir = wordlistDir.replace("*","")
    for root, dirs, files in os.walk(wordlistdir):
        for file in files:
            if (root == wordlistdir):
                wordlist = root + file
            else:
                wordlist = root +"/"+ file
            
            print("Loading: " + wordlist)
            crackpwds(rule, wordlist, crule)

def createRuleList():
    #---------------------------------NOTE---------------------------------
    #o3 and i3 have been added to the extrarules to exclude due to the 
    #extreme length of time it takes to load and run these rules.
    #Remove them from the array if you would like to include them in the
    #use of the * option during rule selection
    #
    #Also note that the o1,i1,o2,i3 rules are removed because they are part
    #of the oi rule set.
    #---------------------------------NOTE---------------------------------
    extrarules = ["best64","d3ad0ne","dive","InsidePro","T0XlC","rockyou-30000","specific","o","i","i1","i2","o1","o2","o3","i3"]
    readConf()
    print("\r\nIf you want to run all the rules listed, enter * and press enter")
    print("If you want to run some rules, comma separate the numbers and press enter\r\n")
    if (isChained):
        val = input("Enter the numbers of the rules separeted by a comma. The first rule will be set for --rules and the rest will be assigned to --rules-stacked: ")
    else:
        val = input("Enter the number(s) of the rule to run: ")
    
    if (isChained):
        try:
            listNumberRule = val.split(",")
            rules = []  

            for ruleNumber in listNumberRule:
                if (ruleNumber.isnumeric() and int(ruleNumber) >= 0 and int(ruleNumber) <= len(ruleList)):
                    rules.append(ruleList[int(ruleNumber)])
            
            crule = rules[0]            
            rules.pop(0)
            rule = ','.join(rules)
            print("\r\nRule: " + crule + " and " + rule + " (as stacked rules)")
            if (isWordlists):
                loopCrack(rule, crule)
            else:
                crackpwds(rule, wordlist, crule)
        except:
            print("unable to split and run jtr")
            exit();
    elif ("," in val):
        try:
            listNumberRule = val.split(",")
            for ruleNumber in listNumberRule:
                if (ruleNumber.isnumeric() and int(ruleNumber) >= 0 and int(ruleNumber) <= len(ruleList)):
                    rule = ruleList[int(ruleNumber)]
                    print("\r\n" + rule + " ruleset will be used")
                    if (isWordlists):
                        loopCrack(rule, None)
                    else:
                        crackpwds(rule, wordlist, None)
        except:
            print("unable to split and run jtr")
            exit();    
    elif (val == "*"):
        for r in ruleList:
            if (r not in extrarules):
                print("Rule: " + r)
                if (isWordlists):
                    loopCrack(r, None)
                else:
                    crackpwds(r,wordlist, None)
    elif (val.isnumeric() and int(val)>=0 and int(val) <= len(ruleList)):
        rule = ruleList[int(val)] 
        print("\r\n" + rule + " ruleset will be used")
        if (isWordlists):
            loopCrack(rule, None)
        else:
            crackpwds(rule, wordlist, None)
    else:
        exit()

def updateShell():
    if (not isUpdateMaster):
        return
 
    print("\r\nUpdating Master Wordlist")
    potpy.process_potfile()
    print("Update Completed\r\n")

def crackpwds(rule, wordlist, crule):
    global isRunning
    isRunning = True
    if (crule is None):
        stackedRule=""
        r = rule
    else:
        stackedRule = " --rules-stack:" + rule + " "
        r = crule
    for hashFormat in hashFormats.split(","):
        if (int(johnFork) <= 1):
            cmdToRun = jtrLocation + " " + hashFile + " --min-length:" + minlength + " --max-length:" + maxlength + " --wordlist:" + wordlist + " --format:" + hashFormat + " --rules:" + r + stackedRule + " --force-tty --no-log --session=" + jtrsession
            print("\r\nRunning:\r\n" + cmdToRun + "\r\n");
            subprocess.call(cmdToRun, shell = True)
        else:
            cmdToRun = jtrLocation + " " + hashFile + " --min-length:" + minlength + " --max-length:" + maxlength + " --wordlist:" + wordlist + " --format:" + hashFormat + " --rules:" + r +  stackedRule + " --fork:" + johnFork + " --force-tty --no-log --session=" + jtrsession
            print("\r\nRunning:\r\n" + cmdToRun + "\r\n");
            subprocess.call(cmdToRun, shell = True)
     
    updateShell()

def displayConfig():
    print("Session Name: " + jtrsession)
    print("Min-Length: " + minlength)
    print("Max-Length: " + maxlength + "\r\n")

def main():
    displayConfig()
    updateShell()
    setJohnFork()
    verifyPaths()
    createRuleList()
    
def verifyPaths():
    global wordlistDir
    if (isWordlists):
        wordlistDir = wordlist.replace("*","")
        if (os.path.exists(wordlistDir) == False):
            print("The wordlist directory could not be found:" + wordlistDir + "\r\nExiting")
            exit()
    else:
         if (os.path.exists(wordlist) == False):
            print("The wordlist could not be found:" + wordlist + "\r\nExiting")
            exit()
    
    if (os.path.exists(hashFile.replace("*","")) == False):
        print("Hash file(s) could not be found:" + hashFile + "\r\nExiting")
        exit()

def handler(signal_received, frame):
    try:
        if (isRunning):
            print("\r\n\r\nAborting current john wordlist/rule\r\nIf another wordlist is available, cracking will continue.\r\n")
        else:
            exit(0)
    except:
        exit(0)

if __name__ == '__main__':
    #Signal implementation is really ugly, need to revist
    #Use ctrl+c during cracking to skip a wordlist/rule combination
    #hold ctrl+c during cracking to kill jtr and this script
    signal.signal(signal.SIGINT, handler)
    print("___________________________________________________________")
    print("       _ __             __         __               ")
    print("      (_) /______      / /_  ___  / /___  ___  _____")
    print(r"     / / __/ ___/_____/ __ \/ _ \/ / __ \/ _ \/ ___/")
    print("    / / /_/ /  /_____/ / / /  __/ / /_/ /  __/ /    ")
    print(r" __/ /\__/_/        /_/ /_/\___/_/ .___/\___/_/     ")
    print("/___/                           /_/                 ")
    print("\r\njtr-helper 1.31")
    print("Ensure Configurations are set for jtr-helper.py")
    print("    set values for: johnConf, johnLocalConf, jtrLocation\r\n")
    print("                 __             ")
    print("    ____  ____  / /_____  __  __")
    print(r"   / __ \/ __ \/ __/ __ \/ / / /")
    print("  / /_/ / /_/ / /_/ /_/ / /_/ / ")
    print(r" / .___/\____/\__/ .___/\__, /  ")
    print("/_/             /_/    /____/   ")
    print("\r\npotpy 1.11")
    print("Ensure Configurations are set for potpy.py")
    print("    set values for potfiles, wordlist_dir, finalFileName")
    print("___________________________________________________________\n\n")
    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--build", help="run just -b to update the master wordlist",action='store_const', const=True)
    parser.add_argument("-f", "--format", help="specify the jtr hash format")
    parser.add_argument("-w", "--wordlist", help="specify the file with wordlist")
    parser.add_argument("-r", "--recursive", help="used with wordlists if a directory is defined: -w /wordlistDIR/*",action='store_const', const=True)
    parser.add_argument("-hash", "--hashes", help="specify the file with hashes")
    parser.add_argument("-min", "--minlength", help="specify the min-length")
    parser.add_argument("-max", "--maxlength", help="specify the max-length")
    parser.add_argument("-session", "--session", help="specify the session")
    parser.add_argument("-s", "--script", help="used to update master list",action='store_const', const=True)
    parser.add_argument("-c", "--chained", help="used to chain rules instead of looping",action='store_const', const=True)
    
    args = parser.parse_args()
    if (args.build):
        isUpdateMaster = True
        updateShell()
        exit()
    if args.format and args.wordlist and args.hashes:
        hashFormats=args.format
        wordlist = args.wordlist
        hashFile = args.hashes
        isUpdateMaster = True if args.script is not None else False
        isChained = True if args.chained is not None else False
        if (args.recursive is None and "/*" in args.wordlist):
            print("You must specify a wordlist file. \r\n* can not be used with out the -r option for wordlist.\r\nPlease correct: " + args.wordlist)
            exit()
        elif (args.recursive and "/*" in args.wordlist):
            isWordlists = True
        else:
            isWordlists = False
        ruleList = []
    else:
        parser.print_help()
        exit()
    if (args.minlength is None):
        minlength='8'
    else:
        minlength=args.minlength
        
    if (args.maxlength is None):
        maxlength='24'
    else:
        maxlength=args.maxlength   
    
    if (minlength.isnumeric() == False):
        print("Please enter a number for minlength")
        exit()
    if (maxlength.isnumeric() == False):
        print("Please enter a number for maxlength")
        exit()
    
    if (args.session is None):
        jtrsession = setSessionIfNull()
    else:
        jtrsession=args.session
    
    main()
