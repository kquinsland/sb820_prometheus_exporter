# Authentication

With no valid session, the modem displays a basic login page.

The two JS functions that do auth are:

```js
function authenticateUser(user, password)
{
    var token = user + ":" + password;

    // Base64 Encoding -> btoa
    var hash = btoa(token); 

    var auth = hash;
    sessionStorage.setItem('auth', auth);
    return auth;
}

function validate( form )
{
        $(".spacer40")[0].innerHTML = "";
        authenticateUser(form.username.value, form.password.value);
        localStorage.setItem('username', form.username.value);
        eraseCookie("sessionId");
        $.ajax({
            type: 'GET',
            url:"/cmconnectionstatus.html?login_" + sessionStorage.getItem('auth'),
            contentType: 'application/x-www-form-urlencoded; charset=utf-8',
            xhrFields: {
                withCredentials: true
            },
            headers: {
                'Authorization': 'Basic ' + sessionStorage.getItem('auth')
            },
            success: function (result) {
                var token = result;
                sessionStorage.setItem('csrftoken', token);
                window.location.href = "/cmconnectionstatus.html?ct_" + token;
            },
            error: function (req, status, error) {
                window.location.href = "/index.html";
            }
        });
}
```

Where clicking the login button calls `validate` with the form data.

So it looks like the modem uses `Basic:` auth and ALSO CSRF.

This is taken from `login_page.html` as seen from the FF web inspector:

```js
<script>
function authenticateUser(user, password)
{
    var token = user + ":" + password;

    // Base64 Encoding -> btoa
    var hash = btoa(token); 

    var auth = hash;
    sessionStorage.setItem('auth', auth);
    return auth;
}

function validate( form )
{
        $(".spacer40")[0].innerHTML = "";
        authenticateUser(form.username.value, form.password.value);
        localStorage.setItem('username', form.username.value);
        eraseCookie("sessionId");
        $.ajax({
            type: 'GET',
            url:"/cmconnectionstatus.html?login_" + sessionStorage.getItem('auth'),
            contentType: 'application/x-www-form-urlencoded; charset=utf-8',
            xhrFields: {
                withCredentials: true
            },
            headers: {
                'Authorization': 'Basic ' + sessionStorage.getItem('auth')
            },
            success: function (result) {
                var token = result;
                sessionStorage.setItem('csrftoken', token);
                window.location.href = "/cmconnectionstatus.html?ct_" + token;
            },
            error: function (req, status, error) {
                window.location.href = "/index.html";
            }
        });
```

So it looks like any time you request a page with the `login_...` query param set, the modem sends back a string. That string is your CSRF token,

Two requests to the same `cmconnectionstatus.html` endpoint are made, one w/ credentials that are redeemed for session and csrf tokens and then the the second request is made with the csrf token to get the useful data.

A little odd but it works.

Will need to see if the CSRF token is used in any other requests / how that's handed off if I want to log in once and then scrape info from a few different pages

## Sample HTML

Two fragments of HTML that came from the modem; used for building beautiful soup functions with

[`connection_status.html`](./connection_status.html): the main page that shows the connection status / the most important data that I'm interested in

[`product_info.html`](./product_info.html): the page that shows the modem's product info, including the serial number and the firmware version and uptime

## Random logouts

I am by no means a front-end dev so I could be missing something totally obvious but I am seeing super inconsistent behavior with requesting pages after log in.

I can authenticate and am brought to the main connections status page.
I can reliably go to the LAG cfg or even the "configuration" (quotes, because it's read only...) and see the page but when I click to event log or product info, 99 our of 100 times I am brought back to the login page.

Not sure why this is; I might have better luck trying to log in, get the CSRF/Session cookie and then immediately request the `cmswinfo.html` or `cmeventlog` page manually.

In browser, I can reliably? get access to product info IF i go to change user password or other page FIRST and then navigate my way over to event log or product information.

Other than a count by type of event level/number, there's not much in the way of metrics there.
The event log data is more useful for scrape -> forward to ELK stack or something similar.
