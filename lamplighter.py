#!/usr/bin/env python

"""
Lamplighter: A presence manager based on network status.

Lamplighter will periodically search the network for specific MAC
addresses. If the MAC addresses are not found, it will trigger a
callback, which is illustrated to switch off your Philips Hue lights,
but in reality can do any number of things. When at least one MAC
address appears, another callback is triggered, which is illustrated
to turn the lights back on.

Using Lamplighter is simple: create a dispatcher script based off of
dispatcher_example.py and keep that script running somehow. I like
Supervisor, but any daemon management system will suffice.

A couple of options must be set; rename config_example.ini to
config.ini and set them there. You can piggyback your own options onto
that file and access the settings from your dispatcher (see
dispatcher_example.py for more).
"""

from ConfigParser import SafeConfigParser
import config
import datetime
import os
import signal
import subprocess
import sys
import time
import urllib

# Default callbacks, which do nothing.
on_home = lambda: None
on_away = lambda: None

# Definition of log levels.
LOG_NONE  = 0
LOG_WARN  = 1
LOG_INFO  = 2
LOG_DEBUG = 3

# Just a couple of globals for aggregating stats.
scans         = 0
state_changes = 0

def run():
    create_pidfile()
    config.load()
    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGHUP, handle_hup)
    start_time  = datetime.datetime.now().replace(microsecond = 0)
    last_report = None
    
    log("Lamplighter has started.", LOG_NONE)
    log("Logging level is set to %s." % config.config["log_level"], LOG_NONE)
    
    if config.config["report_frequency"] is 0 or \
       globals()[config.config["log_level"]] < LOG_WARN:
        log("A summary report will not be logged.", LOG_NONE)
    else:
        log("A summary report will be logged every %s seconds." % config.config["report_frequency"], LOG_NONE)
    
    while True:
        last_report = maybe_print_stats(start_time, last_report)
        search()
        time.sleep(1)
        
def maybe_print_stats(start_time, last_report):
    global scans
    global state_changes
    
    if config.config["report_frequency"] is 0:
        return

    now = int(datetime.datetime.now().strftime("%s"))
    if last_report is None or now - last_report > config.config["report_frequency"]:
        last_report = int(datetime.datetime.now().strftime("%s"))
        running_for = datetime.datetime.now().replace(microsecond = 0) - start_time
        log("Running for %s. Performed %s scan(s), changed state %s time(s)." % (running_for,
                                                                                   scans,
                                                                                   state_changes),
            LOG_WARN)
        
    return last_report
        
def search():
    """The main thread."""
    quiet_hours = ""
    state = current_state()

    if within_quiet_hours():
        quiet_hours = " (quiet hours)"
    log("Commencing search%s" % quiet_hours, LOG_INFO)

    device_count = False
    confirm_with_arp = state == "home" or state == False
    while device_count is False:
        log("Finding initial device count...", LOG_DEBUG)
        device_count = count_devices_present(confirm_with_arp = confirm_with_arp)
        
    if state == False:
        log("No current state. Initializing.", LOG_DEBUG)

        if device_count is 0:
            log("State initialized to away.")
            state = "away"
        else:
            log("State initialized to home.")
            state = "home"

        save_state(state)
    else:
        log("Current state is %s." % state, LOG_DEBUG)

    # Either due to wireless network blips or general unreliability of
    # a single network scan, these scans are guaranteed to be correct
    # about finding any given device, but also very likely to be
    # incorrect about *not finding* devices. What this means is that a
    # transition from "away" to "home" can be done with confidence; if
    # any device is seen on the network, we can be certain that it is
    # real. However, transitions from "home" to "away" must be done
    # more cautiously, lest you have all of the lights in your house
    # turn off and back on repeatedly while you're there (which
    # happened to me, a lot).
    #
    # This seems to be a fairly good compromise between accuracy and
    # complexity: if nmap finds no devices, ask arp-scan to look
    # around. Many times, arp-scan will find a device and the search
    # can be called off. If arp-scan also finds no devices, we wait
    # ten seconds and repeat the whole search three more times (that's
    # six total network scans). If nothing is found all six times,
    # we'll transition to "away."
    if state == "home" and device_count is 0:
        # Delay ten seconds and then check three more times.
        log("*** Possible change to away; wait 10 sec. " + \
            "and search 3 more times...", LOG_INFO)
        time.sleep(10)

        if confirm_device_count_is_zero():
            log("State changed to away.", LOG_WARN)
            save_state("away")

            if within_quiet_hours():
                log("Triggered on_away callback for quiet hours.", LOG_INFO)
                on_away(True)
            else:
                log("Triggered on_away callback.", LOG_INFO)
                on_away(False)

    elif state == "away" and device_count > 0:
        log("State changed to home.", LOG_WARN)
        save_state("home")

        if within_quiet_hours():
            log("Triggered on_home callback for quiet hours.", LOG_INFO)
            on_home(True)
        else:
            log("Triggered on_home callback.", LOG_INFO)
            on_home(False)
        
    else:
        log("State is '%s', device count is %s; nothing to do." % (state, device_count), LOG_INFO)

def within_quiet_hours():
    now = datetime.datetime.now()
    
    # The config module does not know nor care what the values within
    # the config file are, nor their types. We'll get strings for
    # everything, so coerce them into ints so we can compare them.
    start = int(config.config['quiet_hours_start'])
    end = int(config.config['quiet_hours_end'])
    
    if start is 0 and end is 0:
        return False

    # Quiet hours is a range within the same day.
    if start < end and \
       start <= now.hour and end > now.hour:
        return True

    # Quiet hours is a range spanning a day change (through midnight).
    if start > end and \
       (start <= now.hour or end > now.hour):
        return True
        
def confirm_device_count_is_zero():
    log("*** Performing 3 confirmation searches...", LOG_DEBUG)

    for x in range(3):
        test = count_devices_present(confirm_with_arp = True)
        log("*** Found %s device(s)." % test, LOG_DEBUG)

        if test is not 0:
            log("*** False alarm, device(s) found.", LOG_INFO)
            return False

        time.sleep(5)

    return True

def log(message, level = LOG_WARN):
    """Output a pretty log message."""
    if globals()[config.config["log_level"]] >= level:
        pid = os.getpid()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print "[%s] %s: %s" % (pid, now, message)

def state_file_path():
    """Return the path to the state file."""
    return  "/tmp/welcome_home_state"

def save_state(state):
    """Save the given state to disk."""
    statefile_name = state_file_path()
    if os.path.isfile(statefile_name):
        os.unlink(statefile_name)
    file(statefile_name, "w").write(state)
    state_changes += 1

def current_state():
    """Find the current (last saved) state."""
    statefile_name = state_file_path()
    if os.path.isfile(statefile_name):
        statefile = open(statefile_name, "r")
        statefile.seek(0)
        state = statefile.readline().rstrip("\n")
        return state
    else:
        return False

def get_pidfile_name():
    """Return the name of our pid file."""
    return str("/var/run/lamplighter.pid")

def handle_hup(signum, frame):
    log("Received SIGHUP, reloading config file.", LOG_WARN)
    config.load()

def handle_term(signum, frame):
    """Clean up and exit."""
    log("Received SIGTERM; cleaning up and exiting.", LOG_WARN)
    os.unlink(get_pidfile_name())
    sys.exit(0)

def create_pidfile():
    """Create a pidfile for this process."""
    pid = str(os.getpid())
    log("Creating pidfile for %s" % pid, LOG_DEBUG)
    file(get_pidfile_name(), "w").write(pid)

def count_devices_present(confirm_with_arp = False):
    """
    Count devices on the network. Return the count, or False on error.

    The only error that causes a False return value is a non-zero exit
    status from the external program used; if False is returned, it's
    a good idea to call this function again.

    If CONFIRM_WITH_ARP is True, do an additional arp scan when the
    nmap scan returns a zero result, as a means of seeking additional
    confirmation of the zero value.
    """

    count = count_devices_present_nmap()

    if confirm_with_arp and count is 0:
        log("nmap returned zero; waiting one second and confirming with arp.", LOG_DEBUG)
        time.sleep(1)
        count = count_devices_present_arp()

    return count
    
def count_devices_present_arp():
    global scans
    log("Searching for devices with arp-scan.", LOG_DEBUG)
    try:
        device_search = subprocess.check_output(["sudo",
                                                 "arp-scan",
                                                 "192.168.10.0/24"])
        scans += 1
    except subprocess.CalledProcessError:
        log("arp-scan returned a non-zero exit status!", LOG_WARN)
        return False

    return count_devices_in_string(device_search)
    
def count_devices_present_nmap():
    global scans
    log("Searching for devices with nmap.", LOG_DEBUG)
    try:
        device_search = subprocess.check_output(["sudo",
                                                 "nmap",
                                                 "-sn",
                                                 "-n",
                                                 "-T5",
                                                 "192.168.10.0/24"])
        scans += 1
    except subprocess.CalledProcessError:
        log("nmap returned a non-zero exit status!", LOG_WARN)
        return False

    return count_devices_in_string(device_search)

def count_devices_in_string(search_string):
    count = 0
    for name in config.config['devices']:
        if search_string.lower().find(config.config['devices'][name].lower()) > -1:
            log("Found %s's device." % name.title(), LOG_INFO)
            count += 1

    return count

if __name__ == "__main__":
    print "This is the main Lamplighter module. Import it to use it."
