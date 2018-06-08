#!/bin/bash

# Uses sed to enable/disable the first matching item in the given file
# $1= old enabled value
# $2= new enabled value
# $3= full path to file to change
function setEnabled() {
  changeConfig 'enable' $1 $2 $3
}

# Uses sed to enable/disable the first matching item in the given file
# $1= old enabled value
# $2= new enabled value
# $3= full path to file to change
function changeConfig() {
  param=$1
  oldVal=$2
  newVal=$3
  file=$4
  $(sed -i "s~${param}=${oldVal}~${param}=${newVal}~" ${file})
}

# Shutsdown the AdminService (optionally restart it)
# $1= if 1, back up the log file and start it back up again
function shutdown() {
  restart=$1
  
  echo "Shutting down adminservice"
  rhadmin shutdown
  sleep 5

  if [ $restart == $RESTART ]; then
    mkdir -p /var/log/redhawk/old
    tmpFile=$(mktemp /var/log/redhawk/old/adminserviced.log.XXXXXXX)
    mv /var/log/redhawk/adminserviced.log $tmpFile
    echo "Starting adminservice"
    sudo systemctl start redhawk-adminservice.service
    sleep 5
    grep "Didn't expect" /var/log/redhawk/adminserviced.log
    if [ $? == 0 ]; then
      echo "ABORT!!! pid files got erased!"
      exit 1
    fi
  fi
}

function reload() {
  echo "Reloading config"
  rhadmin reload >>/dev/null
  sleep 5
  #rhadmin status
}

# Start the given REDHAWK domain name
# $1= domain name to start
function startDomain() {
  domain=$1
  echo "Starting domain: ${domain}"
  rhadmin start "${domain}:"
  sleep 5
  rhadmin status domain ${domain} | grep RUNNING
  if [[ $? == 1 ]]; then
    echo "ABORT! Domain Manager didn't start"
    exit 1
  fi
}

# Stop the given REDHAWK domain name
# $1= domain name to stop
function stopDomain() {
  domain=$1
  echo "Stopping domain: ${domain}"
  rhadmin stop "${domain}:"
  sleep 5
  rhadmin status domain ${domain} | grep -e STOPPED -e DISABLED
  if [[ $? == 1 ]]; then
    echo "ABORT! Domain Manager didn't stop"
    exit 1
  fi
}

# Checks if the domain manager for the given domain is running
# $1= domain name to check
function domMgrRunning() {
  domain=$1
  ret=`isRunning $domain, 'DomainManager'`
  return $ret
}

# Checks if the device manager with the given name is running
# $1= node name to check
function devMgrRunning() {
  node=$1
  ret=$(isRunning $node, 'DeviceManager')
  return $ret
}

# Checks if the device manager with the given name is running
# $1= node name to check
function compHostRunning() {
  wave=$1
  ret=$(isRunning $wave, 'ComponentHost')
  return $ret
}

# greps against ps to see if the given command and process name is running
# $1= process name to check
# $2= command name to check
function isRunning() {
  name=$1
  proc=$2
  ps aux | grep ${proc} | grep ${name} | grep -v grep
  return $?
}

