#!/bin/bash
source ./test_func.sh

readonly RESTART=1

startingDomain=$(cat /etc/redhawk/domains.d/domain.ini)

echo
echo "Testing domain config from dmd.xml"
testDomain=$(rhadmin config domain $SDRROOT/dom/domain/DomainManager.dmd.xml TEST_DOMAIN)
if [[ "$testDomain" != *"DOMAIN_NAME=TEST_DOMAIN"* ]]; then
  echo "rhadmin config failed to use the given domain name"
  echo $testDomain
  exit 1
fi

echo
echo "Testing domain config from existing .ini"
tmpDir=$(mktemp -d /tmp/adminservicetest.XXXXXXX)
pushd $tmpDir >> /dev/null
echo "${startingDomain}" > domain.ini
cp $SDRROOT/dom/domain/DomainManager.dmd.xml .
testDomain=$(rhadmin config domain $tmpDir/DomainManager.dmd.xml TEST_DOMAIN)
if [[ "$testDomain" != *"DOMAIN_NAME=TEST_DOMAIN"* ]]; then
  echo "rhadmin config failed to use the given domain name"
  echo $testDomain
  exit 1
fi

# Make sure the actual content is the same
expectedDomain=${startingDomain//REDHAWK_DEV/TEST_DOMAIN}
expected="${expectedDomain//[$'\t\r\n ']}"
actual="${testDomain//[$'\t\r\n ']}"
if [[ "${expected}" != "${actual}" ]]; then
  echo "Unexpected domain.ini contents"
  echo "${expected}"
  echo "${actual}"
  exit 1
fi
popd >> /dev/null
\rm -r $tmpDir



##
## Test Nodes
##
startingNode=$(cat /etc/redhawk/nodes.d/node.ini)

echo
echo "Testing node config from dcd.xml"
testNode=$(rhadmin config node $SDRROOT/dev/nodes/Node/DeviceManager.dcd.xml TEST_DOMAIN)
if [[ "$testNode" != *"DOMAIN_NAME=TEST_DOMAIN"* ]]; then
  echo "rhadmin config failed to use the given domain name"
  echo $testNode
  exit 1
fi

echo
echo "Testing node config from existing .ini"
tmpDir=$(mktemp -d /tmp/adminservicetest.XXXXXXX)
pushd $tmpDir >> /dev/null
echo "${startingNode}" > node.ini
cp $SDRROOT/dev/nodes/Node/DeviceManager.dcd.xml .
testNode=$(rhadmin config node $tmpDir/DeviceManager.dcd.xml TEST_DOMAIN)
if [[ "$testNode" != *"DOMAIN_NAME=TEST_DOMAIN"* ]]; then
  echo "rhadmin config failed to use the given node name"
  echo $testNode
  exit 1
fi

# Make sure the actual content is the same
expectedNode=${startingNode//REDHAWK_DEV/TEST_DOMAIN}
expected="${expectedNode//[$'\t\r\n ']}"
actual="${testNode//[$'\t\r\n ']}"
if [[ "${expected}" != "${actual}" ]]; then
  echo "Unexpected node.ini contents"
  echo "${expected}"
  echo "${actual}"
  exit 1
fi
popd >> /dev/null
\rm -r $tmpDir


##
## Test Waveforms
##
startingWave=$(cat /etc/redhawk/waveforms.d/max.ini)

echo
echo "Testing waveform config from sad.xml"
testWave=$(rhadmin config waveform $SDRROOT/dom/waveforms/max/max.sad.xml TEST_DOMAIN)
if [[ "$testWave" != *"DOMAIN_NAME=TEST_DOMAIN"* ]]; then
  echo "rhadmin config failed to use the given domain name"
  echo $testWave
  exit 1
fi

echo
echo "Testing waveform config from existing .ini"
tmpDir=$(mktemp -d /tmp/adminservicetest.XXXXXXX)
pushd $tmpDir >> /dev/null
echo "${startingWave}" > node.ini
cp $SDRROOT/dom/waveforms/max/max.sad.xml .
testWave=$(rhadmin config waveform $tmpDir/max.sad.xml TEST_DOMAIN)
if [[ "$testWave" != *"DOMAIN_NAME=TEST_DOMAIN"* ]]; then
  echo "rhadmin config failed to use the given domain name"
  echo $testWave
  exit 1
fi

# Make sure the actual content is the same
expectedWave=${startingWave//REDHAWK_DEV/TEST_DOMAIN}
expected="${expectedWave//[$'\t\r\n ']}"
actual="${testWave//[$'\t\r\n ']}"
if [[ "${expected}" != "${actual}" ]]; then
  echo "Unexpected waveform.ini contents"
  echo "${expected}"
  echo "${actual}"
  exit 1
fi
popd >> /dev/null
\rm -r $tmpDir

