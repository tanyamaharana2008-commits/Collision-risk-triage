# Kessler Eye: Orbital Close-Approach Monitor

## One-line pitch
Kessler Eye predicts when two objects in orbit will pass dangerously close, ranks those encounters by risk, and gives operators one dashboard to triage them. It works on a laptop or a phone.

## The problem
Low Earth orbit is crowded with active satellites, dead satellites and debris, all moving at several kilometres per second. A collision creates thousands of new fragments, which raises the chance of more collisions. This chain reaction is called the **Kessler syndrome**. Operators face tens of thousands of predicted close approaches, and they need to know which few deserve attention first.

 What we built
A pipeline and dashboard that take orbital data through to a decision:

1. Detect:find predicted close approaches between tracked objects. Each one has a time of closest approach, a miss distance and a relative speed. *(Fill in your data source and detection method from Stages 1–4.)*
2. Score: a risk model (Stage 5) gives every approach a 0–100 score. Up to 60 points come from how small the miss distance is (zero beyond 2 km), and the rest from how soon it happens and how fast the objects approach each other. Scores of 70 or more are High and 40 or more are Medium. Anything missing by more than 2 km stays Low.
3. Store: PostgreSQL holds the objects, events, risk assessments and alerts. Our database currently holds 59,666 predicted approaches.
4. Act: the web dashboard (Stage 8) lets an operator review, filter and decide.

 Dashboard features
- Risk overview: a headline of how many upcoming approaches rank high, plus a time-versus-miss-distance chart. High-risk approaches glow pink, medium are amber and low are faint blue.
- Live countdown to the next high-risk approach.
- Did it collide? Every passed approach gets a verdict: collision predicted, very close pass, near miss or clear pass. Upcoming ones show whether an impact is predicted.
- Triage: Escalate, Monitor or Dismiss each event, saved to the database, with a filter by decision.
- What-if uncertainty slider (×0.5 to ×3.0): re-ranks everything using Stage 5's own formula, showing how the picture changes if the true miss distance is smaller than reported.
- Alerts with unread tracking and mark-as-read.
- Search by object name or NORAD ID, sort, and CSV export of any view.
- Function keys (F1–F10) and keyboard shortcuts for fast operation.
- Phone access: the layout is responsive, and the app can be opened on a phone through a password-protected link.
- Times show in IST with UTC alongside.

 Technology
Python (standard-library web server), PostgreSQL, and a single-page interface in plain JavaScript with a canvas chart. There are no heavy frameworks, so it runs anywhere Python does.

 Honest limits
These are good to state before a judge asks.
- Scores **rank** predicted close approaches. They are not collision probabilities.
- The collision verdict compares the predicted miss distance with an assumed 20 m combined object size, so it is a prediction. The app has no tracking data from after each event to confirm what happened.
- The risk model is a prototype, and its weights could be tuned against real conjunction data.
