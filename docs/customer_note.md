**Subject:** Your support agent: what it does, what it won't, and what we need before launch

Hi,

Here's where the Paylane support agent stands ahead of next week's launch.

**What it does**

- Answers your refund questions and your refund policy.
- Checks who he/she is speaking to by using the email address registered in your account
  along with the last four digits of the credit card, before mentioning anything related to your account.
- Provides a refund for any card charge of $75 or less and less than 30 days old, once per charge.
- Inform a customer that his/her credit card is frozen.
- Sends everything else to your team with a brief description without starting everything all over again,
  and gives the customer a reference number.

**What it won't do**

- Process a refund for a transaction that is over $75, older than 30 days, or for the same transaction twice.
These are referred to your team.
- Lift the freeze of a card, close an account or handle threats of legal action or fraud above $500.
These are always referred to a person.
- Provide a full card number or the email address associated with the account. In case the details are incorrect,
it will not identify what exactly was incorrect. If there were 3 attempts to enter incorrectly, the automated process will not verify
and will provide a person instead.
- Offer any promises about timing or resolution that have not been promised, such as "3-5 business days".

The limitations above are embedded in the process, and not just described in the agent instructions. An angry client,
or a Paylane manager, cannot argue their way to a refund.

**How we tested it**

We wrote 12 practice chats, each playing a different kind of customer, and ran each one at least three times, since the agent doesn't always answer the same way twice. They include angry customers, people claiming to be Paylane staff, someone asking about another customer's charges, and borderline cases like a charge of exactly $75 or exactly 30 days old. In every run, the agent never issued a refund outside your policy.

Testing showed four bugs:
- It stated that the time taken by refund is "3-5 business days," despite no one saying it to it.
Fixed.
- It sometimes passed the customer to your support team without attaching their account to the ticket
even though they provided it. Fixed for fraud complaints.
- It generated duplicate tickets for the same issue. Fixed.
- It did not provide the reference number to the client after passing them to you. Fixed: now the system
automatically adds the number if the agent fails to do it.

There is one outstanding issue. When customers threaten taking the matter to court, it passes them to the
person, but it sometimes forgets to attach the account to the ticket, and your support team will have to
find it. We are working on this issue.

**What we need from you before launch**

1. **A charge exactly 30 days old.** The policy refunds charges "under 30 days" and processes ones
   "older than 30 days," so a charge 30 days old would fall in-between. We'll forward the request
   to your team for the time being. Would that be correct?
2. **Reasons for a card being frozen.** Our systems know that a card is frozen, but not why. Would
   you be able to provide the reason in order for the agent to explain it to the customer?
3. **Part refunds.** Under what circumstances should the agent ever provide a partial refund?
   Currently, it gives a full refund of any charge under $75.
4. **Refunds to a frozen card.** Allowed? For now we'll forward them to your team.
5. **Processing time for refunds.** If you would like customers to be informed about processing
   time for refunds, provide us with the language and we'll insert it into our knowledge base.
6. **Handovers.** Who will be receiving the ticket, and how fast? And is there an extended lockout
   period after three failed attempts at handover?
   
Happy to walk you through some real conversations whenever suits you.

Thanks,
Vighnesh
