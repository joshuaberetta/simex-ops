# Rich mail and What's new: plan

Two features, built on `main` step by step.

## 1. Rich message bodies in SIMEX Mail

Participants, team leaders and the persona desk write with formatting (bold, italics,
underline, strikethrough, colour, lists, quotes, links) and put images in the text itself, as
in webmail: pasted, dropped or chosen from a toolbar button.

### Storage

- `Mail.html` and `Message.body_html`: the body as **clean** HTML (`ops/richtext.py`), empty
  when the message has no formatting (then the plain text says it all and nothing changes).
- `Mail.body` / `Message.body` stay the **plain text** (`richtext.to_text`), so search,
  snippets, SIMEX AI, reports, the network, the desk's readings and ChatSIMEX keep working on
  text and never see markup.
- An image in the text is a file of the message with a `cid` (`MailAttachment.cid`,
  `MessageAttachment.cid`); the HTML refers to it only as `src="cid:<cid>"`, which is also how
  an email carries it (Resend attachment `content_id`). Images are PNG, JPEG, GIF or WebP
  only (never SVG); the composer scales big photos down before upload.
- Files with a `cid` are not listed as attachments under the message.

### Safety

The HTML comes from a browser, so it is untrusted. `richtext.clean` rebuilds it from an
allowlist (tags, attributes, a few style properties, `http(s)`/`mailto` links, `cid:` images
only); it runs on save **and** again on display. External images (tracking pixels) and
`data:` images are never kept. Inline images are served by `mail_file` with `?inline=1` only
when they are a `cid` image of an allowed type, `nosniff`, and never count as a download.

### Flow

- Composer (`_composer_fields.html`, `mail.js` `Composer`): a contenteditable editor with a
  toolbar replaces the textarea; it posts `html` and `body` (the text), and its images as
  `inline` files with `inline_cid`. Drafts keep their images (`draft_save` stores them on the
  draft; `send` takes any it doesn't get again from the draft or the forwarded mail).
- `views_inbox.send` → `_send` / `_send_as_leader` / `_send_desk` → `desk.send` →
  `delivery.answer` / `desk._new`: the HTML and inline files travel with the body.
- `render.Draft.body_html` → `Message.body_html`, and the email's HTML part
  (`render.wrap_html`) uses it instead of `text_to_html(body)`.
- Every copy keeps it: `delivery.COPIED` has `html`, `copy_mail` / `_attach` / `desk._files`
  copy `cid`; `_put_mail`, `deliver_personal`, the leader's Sent copy and the desk's copies
  take `html`.
- Display: `thread.html` uses the `richbody` filter (the HTML with each `cid:` mapped to the
  file's URL), else `mailbody` as before.

### Later

- ExCon's own forms (Compose, answering on the reply page, injects) are still plain text.
- Inbound emails' HTML parts (participants answering by email) are still read as text.
- Image size handles in the editor (small / best fit / original).

## 2. What's new

A short announcement of each new feature, shown to everyone it concerns, opened on their
first page load after it ships and then just a dot until they look again.

- `ops/whatsnew.py`: `ENTRIES`, newest first, each a `key`, `date`, `title`, `body`
  (Markdown) and `who` (any of `participant`, `leader`, `desk`, `rp`, `excon`).
- `WhatsNewSeen` (user, key, unique): what each login has seen. `whatsnew.for_user` gives
  the entries for that login and which are new.
- Shown in every base: ExCon's Notifications panel (a What's new section at the top),
  SIMEX Mail's notifications drawer (the bell: the desk's existing one gets the section; every
  other mailbox now has a bell too), the role players' pages. `static/ops/whatsnew.js` opens
  the drawer on load when something is new and posts `/whatsnew/seen/`, so the next load
  doesn't open it.
- Adding an entry is the release note: add one to `ENTRIES` for any change people will notice.
