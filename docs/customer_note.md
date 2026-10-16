**Subject:** Your support agent: what it does, what it won't, and what we need before launch

Hi,

Here's where the Paylane support agent stands ahead of next week's launch.

**What it does**

- Answers general questions about refunds and your policy.
- Checks who it's talking to, using the email on the account and the last 4 digits of the card,
  before it discusses anything about that account.
- Refunds a card charge of $75 or less that is under 30 days old, once per charge.
- Tells a customer when their card is frozen.
- Passes anything else to your team, with a short summary, so nobody has to start from scratch,
  and gives the customer a reference number to quote if they get in touch again.

**What it won't do**

- Refund a charge over $75, a charge 30 days old or older, or the same charge twice. These go to
  your team.
- Unfreeze a card, close an account, or deal with legal threats or fraud over $500. These always go
  to a person.
- Show a full card number or the email on file. If someone gets their details wrong, it doesn't say
  which part was wrong. After three wrong tries it stops checking and offers a person instead.
- Promise timings or outcomes it hasn't been given, such as "3-5 business days".

These limits are built into the system, not just written into the agent's instructions. A customer
who argues, or claims to be a Paylane manager, can't talk it into a refund.

**How we tested it**

We wrote 12 conversations and ran each one several times. They include angry customers, people
claiming to be Paylane staff, someone asking about another customer's charges, and edge cases like
a charge of exactly $75 or exactly 30 days old. The agent never issued a refund outside your
policy.

Testing turned up four problems:
- It was telling customers refunds take "3-5 business days", which nobody told it. Fixed.
- It sometimes passed a customer to your team without attaching their account to the ticket, even
  though they had given their details. Fixed for fraud reports.
- It opened duplicate tickets for the same problem. Fixed.
- It didn't always give the customer their reference number after a handover. Fixed: the system now
  adds it if the agent leaves it out.

One gap is still open. When a customer threatens legal action, it hands them to a person but doesn't
always attach their account, so your team may need to look them up. We're still working on this.

**What we need from you before launch**

1. **A charge exactly 30 days old.** Your policy refunds charges "under 30 days" and passes on ones
   "older than 30 days", so day 30 falls in between. For now we pass it to your team. Is that right?
2. **Why a card is frozen.** We can only see that a card is frozen, not why. Can we get the reason,
   so the agent can explain it?
3. **Part refunds.** Should the agent ever refund part of a charge? Right now it refunds the full
   amount, and only on charges of $75 or less.
4. **Refunds to a frozen card.** Allowed? For now we pass these to your team.
5. **How long refunds take.** If you want customers told, send us the wording and we'll add it.
6. **Handovers.** Who picks up the tickets, and how quickly? And after three wrong tries, should the
   customer be locked out for longer than that one chat?

Happy to walk you through some real conversations whenever suits you.

Thanks,
Vighnesh
