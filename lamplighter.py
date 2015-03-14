#!/usr/bin/env python

"""Lamplighter: Automated light management.

Lamplighter will periodically search the network for specific MAC
addresses. If the MAC addresses are not found, it will switch off
the lights. It does the inverse when at least one MAC address
appears."""

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
on_away = lambda x: x
on_home = lambda x: x

def run():
    create_pidfile()
    config.load()
    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGHUP, handle_hup)
    
    while True:
        search()
        time.sleep(1)
        
def search():
    """The main thread."""
    
    quiet_hours = ""
    if within_quiet_hours():
        quiet_hours = " (quiet hours)"
    log("--- Commencing search%s ---" % quiet_hours)

    state = current_state()
    if state == False:
        log("No current state. Initializing.")

        if device_count is 0:
            log("Current state: away.")
            state = "away"
        else:
            log("Current state: home.")
            state = "home"

        save_state(state)
        
    log("Current state is %s." % state)

    device_count = False
    confirm_with_arp = state == "home"
    while device_count is False:
        log("Finding initial device count...")
        device_count = count_devices_present(confirm_with_arp = confirm_with_arp)
        
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
    # complexity: if it looks like no devices were found, wait ten
    # seconds and then scan for them three more times, waiting for
    # five seconds between each scan. If all three of those scans find
    # nothing, we'll commit to the state change.
    if state == "home" and device_count is 0:
        # Delay ten seconds and then check three more times.
        log("*** Possible change to away; wait 10 sec. and search 3 more times...")
        time.sleep(10)

        if confirm_device_count_is_zero():
            log("Devices confirmed missing. State changed to away.")
            save_state("away")

            if within_quiet_hours():
                log("Within quiet hours! Simply notifying.")
                send_message("It appears you've left, but it's late, and I'm tired. Turn off your own lights.")
            else:
                on_away()
                #lights_off()
                send_message("Lights are now off. Have a good day.")

    elif state == "away" and device_count > 0:
        log("*** State changing to home.")
        save_state("home")

        if within_quiet_hours():
            log("Within quiet hours! Simply notifying.")
            send_message("It appers you've returned home, but it's late, and I have a headache. Go to bed.")
        else:
            on_home()
            #lights_on()
            send_message("Lights are now on. Welcome home.")
        
    else:
        log("State is '%s', device count is %s; nothing to do." % (state, device_count))

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
    log("*** Performing 3 confirmation searches...")

    for x in range(3):
        test = count_devices_present(confirm_with_arp = True)
        log("*** Found %s device(s)." % test)

        if test is not 0:
            log("*** False alarm, device(s) found.")
            return False

        time.sleep(5)

    return True

def log(message):
    """Output a pretty log message."""
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
    log("Received SIGHUP, reloading config file.")
    config.load()

def handle_term(signum, frame):
    """Clean up and exit."""
    log("Received SIGTERM; cleaning up and exiting.")
    os.unlink(get_pidfile_name())
    sys.exit(0)

def create_pidfile():
    """Create a pidfile for this process."""
    pid = str(os.getpid())
    log("Creating pidfile for %s" % pid)
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
        log("nmap returned zero; waiting one second and confirming with arp.")
        time.sleep(1)
        count = count_devices_present_arp()

    return count
    
def count_devices_present_arp():
    log("Searching for devices with arp-scan.")
    try:
        device_search = subprocess.check_output(["sudo",
                                                 "arp-scan",
                                                 "192.168.10.0/24"])
    except subprocess.CalledProcessError:
        log("arp-scan returned a non-zero exit status!")
        return False

    return count_devices_in_string(device_search)
    
def count_devices_present_nmap():
    log("Searching for devices with nmap.")
    try:
        device_search = subprocess.check_output(["sudo",
                                                 "nmap",
                                                 "-sn",
                                                 "-n",
                                                 "-T5",
                                                 "192.168.10.0/24"])
    except subprocess.CalledProcessError:
        log("nmap returned a non-zero exit status!")
        return False

    return count_devices_in_string(device_search)

def count_devices_in_string(search_string):
    count = 0
    for name in config.config['devices']:
        if search_string.lower().find(config.config['devices'][name].lower()) > -1:
            log("Found %s's device." % name.title())
            count += 1

    return count

def send_message(message):
    """Send a text message to my device through Twilio."""
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % config.config['twilio_account_id']
    payload = {
        'Body': message,
        'From': config.config['twilio_outgoing_number'],
    }
    creds = (config.config['twilio_account_id'], config.config['twilio_auth_token'])

    numbers = config.config['twilio_notification_numbers']
    for number in numbers.split(","):
        payload['To'] = number
        response = requests.post(url, data=payload, auth=creds)
        rsdata = response.json()

        log("Message %s to %s '%s' at %s." % (rsdata['sid'],
                                            rsdata['to'],
                                            rsdata['status'],
                                            rsdata['date_created']))

if __name__ == "__main__":
    print "This is the main Lamplighter module. Import it to use it."