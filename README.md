# Govee LAN Control for Home Assistant

This works in conjunction with my
[govee-led-wez](https://github.com/wez/govee-py) python library to provide
control over Govee-manufactured lights, preferentially using the LAN protocol
for local control.

## Installation

Recommended first step: Obtain an HTTP API key from the Govee API:
* Open the Account Page of the Govee mobile app (the person icon in the bottom right)
* Click the settings "cog" icon in the top right
* Click Apply for API Key and fill out the form
* Your key will be emailed to you.

It is recommended to wait until you have the key before configuring the
integration, as the HTTP API is used to retrieve the names of the devices from
your account, and those names influence the entity ids that are set up for the
devices.

You don't require an HTTP API key to use this integration if all of the devices
that you want to control are supported by the LAN API, but having your names
set up from the app is nice, so I recommend getting that set up anyway.

* Install [HACS - the Home Assistant Community Store](https://hacs.xyz/docs/setup/download/)
* Add this repo to HACS by:
  1. Open the HACS integrations page
  2. In the top right corner click the three vertical dots and select "Custom repositories"
  3. Paste in the URL to this repo in the "Repository" box
  4. Set category to "Integration"
  5. Click Add

* Once added, restart Home Assistant
* Then go to Settings -> Devices &amp; Services and click "Add Integration"
* Type "Govee LAN Control" and add the integration
* Enter your HTTP API key where prompted
