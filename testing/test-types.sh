#!/bin/bash
source ./test_func.sh

readonly RESTART=1
startingCond=$(cat /etc/redhawk/rh.cond.cfg)

rhadmin status
echo "Stopping adminservice"
sudo systemctl stop redhawk-adminservice.service

echo
echo "Starting adminservice"
sudo systemctl start redhawk-adminservice.service
sleep 5

echo
rhadmin status

echo
stopDomain "REDHAWK_DEV"

rhadmin status REDHAWK_DEV | grep RUNNING
if [[ $? == 0 ]]; then
  echo "Something is running in the domain, should be nothing"
  rhadmin status
  exit 1
fi

#
# Test starting by type
#
echo
echo "Starting domain by type"
rhadmin start domain REDHAWK_DEV

ret=$(domMgrRunning 'REDHAWK_DEV')
if [[ $ret == 1 ]]; then
  echo "Domain Manager didn't start"
  rhadmin status
  exit 1
fi

echo
echo "Starting nodes by type"
rhadmin start nodes REDHAWK_DEV

ret=$(devMgrRunning 'Node')
if [[ $ret == 1 ]]; then
  echo "Node node didn't start"
  rhadmin status
  exit 1
fi

echo
echo "Starting waveforms by type"
rhadmin start waveforms REDHAWK_DEV

ret=$(compHostRunning 'max')
if [[ $ret == 1 ]]; then
  echo "max waveform didn't start"
  rhadmin status
  exit 1
fi

#
# Test statusing types
#
echo
echo "Statusing domains by type"
rhadmin status domain REDHAWK_DEV | grep 'RUNNING'
if [[ $? == 1 ]]; then
  echo "Error statusing Domain Manager, no results or not running"
  rhadmin status
  exit 1
fi

echo
echo "Statusing nodes by type"
rhadmin status nodes REDHAWK_DEV | grep 'RUNNING'
if [[ $? == 1 ]]; then
  echo "Error statusing nodes, no results or not running"
  rhadmin status
  exit 1
fi

echo
echo "Statusing waveforms by type"
rhadmin status waveforms REDHAWK_DEV | grep 'RUNNING'
if [[ $? == 1 ]]; then
  echo "Error statusing waveforms, no results or not running"
  rhadmin status
  exit 1
fi

#
# Test stopping by type
#
echo
echo "Stopping waveforms by type"
rhadmin stop waveforms REDHAWK_DEV | grep 'RUNNING'
if [[ $? == 0 ]]; then
  echo "Error stopping waveforms, still running?"
  rhadmin status
  exit 1
fi

echo
echo "Stopping nodes by type"
rhadmin stop nodes REDHAWK_DEV | grep 'RUNNING'
if [[ $? == 0 ]]; then
  echo "Error stopping nodes, still running?"
  rhadmin status
  exit 1
fi

echo
echo "Stopping domain by type"
rhadmin stop domain REDHAWK_DEV | grep 'RUNNING'
if [[ $? == 0 ]]; then
  echo "Error stopping Domain Manager, still running?"
  rhadmin status
  exit 1
fi

#
# Test kill -9'ing the domain
#
echo
startDomain "REDHAWK_DEV"
sleep 5

echo
echo "Making sure domain started correctly"
rhadmin status | grep -e STOP -e DISABLED -e BACK
if [[ $ret == 0 ]]; then
  echo "Domain didn't start correctly"
  rhadmin status
  exit 1
fi

ret=$(rhadmin status | grep RUNNING | wc -l)
if [ "${ret}" != "3" ]; then
  echo "Domain didn't start correctly"
  rhadmin status
  exit 1
fi

adminPid=$(ps -ef | grep adminservice | grep root | awk '{print $2}')
echo
echo "Killing adminservice (pid ${adminPid})"
sudo kill -9 $adminPid
sleep 5

if [[ `ps -ef | grep adminservice | grep root | wc -l` != 0 ]]; then
  echo "Didn't kill adminservice correctly"
  ps -ef | grep adminservice
  exit 1
fi

ret=$(domMgrRunning 'REDHAWK_DEV')
if [[ $ret == 1 ]]; then
  echo "Domain Manager got killed?"
  exit 1
fi
ret=$(devMgrRunning 'Node')
if [[ $ret == 1 ]]; then
  echo "Node got killed?"
  exit 1
fi
ret=$(compHostRunning 'max')
if [[ $ret == 1 ]]; then
  echo "max waveform got killed?"
  exit 1
fi

# Store the old pids of the services
domPid=$(ps -ef | grep DomainManager | grep REDHAWK_DEV | awk '{print $2}')
devPid=$(ps -ef | grep DeviceManager | grep REDHAWK_DEV | awk '{print $2}')
wavPid=$(ps -ef | grep ComponentHost | grep max | awk '{print $2}')

echo
echo "Checking status after restarting the adminservice"
sudo systemctl start redhawk-adminservice.service
echo
echo "Waiting for adminservice to start"
sleep 60

ret=$(rhadmin status | grep RUNNING | wc -l)
if [ "${ret}" != "3" ]; then
  echo "Domain didn't start correctly"
  rhadmin status
  exit 1
fi

# Store the old pids of the services
newDomPid=$(ps -ef | grep DomainManager | grep REDHAWK_DEV | awk '{print $2}')
newDevPid=$(ps -ef | grep DeviceManager | grep REDHAWK_DEV | awk '{print $2}')
newWavPid=$(ps -ef | grep ComponentHost | grep max | awk '{print $2}')
if [[ "${domPid}" -ne "${newDomPid}" || "${devPid}" -ne "${newDevPid}" || "${wavPid}" -ne "${newWavPid}" ]]; then
  echo "Pids have changed on restart! At least one of the processes seems to have stopped"
  exit 1
fi
#numFound=$(grep "was already running. Not going to relaunch" /var/log/redhawk/adminserviced.log | wc -l)
#echo "Found ${numFound} services"
#if [[ ${numFound} != 3 ]]; then
#  echo "Problem starting after kill -9, was everything restarted?"
#  rhadmin status
#  exit 1
#fi

#
# Disable the configuration for the max waveform in the .ini file
#
echo
echo "Disabling max"
changeConfig enable 1 "enable=1" "/etc/redhawk/waveforms.d/max.ini"

rhadmin update
sleep 5

# Verify that max is disabled
rhadmin list | grep "max" | grep "Disabled"
max=$?
ret=$(compHostRunning "max")
if [[ $ret == 0 || $max == 1 ]]; then
  echo "max waveform didn't stop or is not disabled"
  rhadmin status
  echo $startingCond > /etc/redhawk/rh.cond.cfg
  exit 1
fi

echo
echo "restarting, is max going to start up?"
shutdown $RESTART
echo "   Waiting for things to start up again"
sleep 35   # Need extra to account for previous stuff starting up

ret=$(compHostRunning 'max')
if [[ $ret == 0 ]]; then
  echo "max waveform started? It's disabled so it shouldn't have"
  rhadmin status
  echo $startingCond > /etc/redhawk/rh.cond.cfg
  exit 1
fi

#
# Enable the configuration for the max waveform in the .ini file
#
echo
echo "Enabling max"
echo "enable=1" >> /etc/redhawk/rh.cond.cfg

reload

# Verify that max is enabled
rhadmin list | grep "max" | grep "Enabled"
max=$?
ret=$(compHostRunning 'max')
if [[ $ret == 1 || $max == 1 ]]; then
  echo "max waveform didn't start?"
  rhadmin status
  echo $startingCond > /etc/redhawk/rh.cond.cfg
  exit 1
fi

echo
echo "   Waiting for max to start up"
var=1
# Loop and wait until max has started
while [ $var == 1 ];
do
  rhadmin status REDHAWK_DEV:max | grep RUNNING
  if [ $? == 0 ]; then
    var=0
  else
    sleep 1
  fi
done

# Restore the old enable value
changeConfig enable "enable=1" 1 "/etc/redhawk/waveforms.d/max.ini"
echo $startingCond > /etc/redhawk/rh.cond.cfg

echo
shutdown $RESTART

exit 0
