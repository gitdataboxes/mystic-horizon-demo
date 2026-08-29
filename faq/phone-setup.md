# Phone Setup

## How do phone calls work?

Phone calls need two services working together: Tailscale and Twilio. Tailscale
creates a secure public tunnel from the internet to your local machine. Twilio
provides a real phone number and routes incoming calls through that tunnel to
your agent. Without both, calls can't reach you — Tailscale is the road and
Twilio is the address.

## What is Tailscale?

Tailscale is a networking tool that can expose your local server to the
internet. Mystic Horizon uses a Tailscale feature called Funnel, which creates
a public HTTPS URL that routes traffic to your local machine. This is how
Twilio delivers phone calls to an agent running on your own computer instead of
a cloud server. Tailscale must be installed, running, and logged in before phone
setup can proceed.

## What is Twilio?

Twilio is a telephony API that provides real phone numbers and handles
call routing. When someone dials your Twilio number, Twilio sends the call to
your agent via webhooks. The agent needs a Twilio Account SID and Auth Token
(credentials from your Twilio account) and a phone number purchased through
Twilio. Twilio has a free trial tier that works fine for getting started.

## What is the phone setup sequence?

Phone setup has four steps that must happen in order:

1. Tailscale must be installed, running, and authenticated. Use the
   check-tailscale skill to verify this.
2. Enter Twilio credentials (Account SID and Auth Token). These get validated
   against the Twilio API. Use write-twilio-credentials to save them.
3. Get a phone number. If the account may already own a number, use
   read-twilio-numbers first, then use write-twilio-number with the chosen
   phone_number to attach it. If they need a new number, use write-twilio-number
   with an area_code to search, then with the selected full phone_number to buy
   it. When Tailscale is ready, write-twilio-number also starts or reuses Funnel
   and verifies the Twilio webhooks.
4. Verify the public line. Use activate-tunnel to retry or confirm Tailscale
   Funnel and Twilio webhook readiness.

Each step depends on the previous one. The read-setup skill shows which steps
are done and what's next.

## How do I install Tailscale?

Run this command in a terminal: `curl -fsSL https://tailscale.com/install.sh | sh`
Then start it with: `sudo tailscale up`
Follow the browser link to log into your Tailscale account. If you don't have
an account, Tailscale offers a free tier. Once logged in, the check-tailscale
skill should report that Tailscale is ready.

## What if Tailscale is installed but not working?

Common issues: the Tailscale daemon might not be running (fix: `sudo tailscale
up`), or you might not be authenticated yet (fix: run `sudo tailscale up` and
follow the login URL). The check-tailscale skill reports exactly what's wrong —
whether the binary is missing, the daemon is stopped, or authentication is
needed.

## Where do I find my Twilio credentials?

Log into your Twilio account at twilio.com. Your Account SID and Auth Token are
on the main dashboard page. The Account SID starts with "AC" and the Auth Token
is a long string of letters and numbers. Copy both exactly — the agent validates
them against the Twilio API before saving.

## What if my Twilio credentials are rejected?

The most common cause is a typo — Account SIDs start with "AC" and are 34
characters long. Double-check you copied the full string. If the credentials
are correct but still fail, your Twilio account might be suspended or the API
might be temporarily down. You can verify your credentials work by logging into
the Twilio console.

## Can I use the agent without phone calls?

Yes. Phone calls are completely optional. You can talk to the agent through the
dashboard using voice chat or text chat without configuring Tailscale or Twilio
at all. The dashboard voice connection uses LiveKit locally and doesn't need any
external services for the voice link itself (though it still needs an LLM
provider and optionally cloud STT/TTS).

## Can I set up phone calls from the command line instead?

Yes. Run `mystic init --connect-twilio` from the terminal. This walks through
the same setup — Tailscale check, Twilio credentials, phone number — in an
interactive CLI flow. It checks Tailscale readiness during setup and saves
everything to config when done.
