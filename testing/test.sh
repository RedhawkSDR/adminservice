#!/bin/bash
source ./test_func.sh

readonly RESTART=1

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
startDomain "REDHAWK_DEV"

#
# Stop the node, make sure it does
#
echo
echo "Stopping Node"
rhadmin stop REDHAWK_DEV:Node
sleep 5

ret=$(devMgrRunning 'Node')
if [[ $ret == 0 ]]; then
  echo "Node node didn't shut down?"
  rhadmin status
  exit 1
fi

#
# Start the node, verify that it does start
#
echo
echo "Starting Node"
rhadmin start REDHAWK_DEV:Node
sleep 15   # Sleep some more to make sure it has time to start

ret=$(devMgrRunning 'Node')
if [[ $ret == 1 ]]; then
  echo "Node node didn't start up?"
  rhadmin status
  exit 1
fi

echo
shutdown $RESTART

echo
startDomain "REDHAWK_DEV"


#
# Disable the configuration for the max waveform in the .ini file
#
echo
echo "Disabling max"
setEnabled 1 0 "/etc/redhawk/waveforms.d/max.ini"

rhadmin update
sleep 5

# Verify that max is disabled
rhadmin list | grep "max" | grep "Disabled"
max=$?
ret=$(compHostRunning "max")
if [[ $ret == 0 || $max == 1 ]]; then
  echo "max waveform didn't stop or is not disabled"
  rhadmin status
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
  exit 1
fi

echo
echo "Check if max will force start"
rhadmin start -f REDHAWK_DEV:max

ret=$(compHostRunning 'max')
if [[ $ret == 1 ]]; then
  echo "max waveform didn't start?"
  exit 1
fi

echo
shutdown $RESTART


#
# Enable the configuration for the max waveform in the .ini file
#
echo
echo "Enabling max"
setEnabled 0 1 "/etc/redhawk/waveforms.d/max.ini"

reload

# Verify that max is enabled
rhadmin list | grep "max" | grep "Enabled"
max=$?
ret=$(compHostRunning 'max')
if [[ $ret == 1 || $max == 1 ]]; then
  echo "max waveform didn't start?"
  rhadmin status
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

echo
echo "stopping max"
rhadmin stop REDHAWK_DEV:max

ret=$(compHostRunning 'max')
if [[ $ret == 0 ]]; then
  echo "max waveform didn't stop?"
  rhadmin status
  exit 1
fi

echo
shutdown $RESTART

#
# Disable the max waveform by renaming it so it isn't an ini file anymore
#
echo
echo "Disabling max 2"
mv /etc/redhawk/waveforms.d/max.ini /etc/redhawk/waveforms.d/max.in

reload

# Verify that max isn't in the configurations anymore
rhadmin list | grep "max"
max=$?
ret=$(compHostRunning 'max')
if [[ $ret == 0 || $max == 0 ]]; then
  echo "max waveform didn't stop or is still available"
  rhadmin status
  exit 1
fi

echo
shutdown $RESTART

#
# Check if reload will start the waveform by copying the file back again
#
echo
echo "Enabling max 2"
mv /etc/redhawk/waveforms.d/max.in /etc/redhawk/waveforms.d/max.ini

reload
echo "   Waiting for things to start up again"
sleep 35   # Need extra to account for previous stuff starting up

# Verify that max is in the configuration again
rhadmin list | grep "max"
max=$?
ret=$(compHostRunning 'max')
if [[ $ret == 1 || $max == 1 ]]; then
  echo "max waveform didn't start or is missing"
  rhadmin status
  exit 1
fi

echo
shutdown $RESTART

exit 0
