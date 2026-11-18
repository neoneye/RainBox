# Room topic registry

A room's chunk summaries name their topics chunk by chunk, with no list of
earlier names to choose from, so the same subject gets a new name in every
chunk. In one room of 16 chunks the 93 topic names of the parts were 79
different names, and 70 of them occurred once. A histogram of those names
shows nothing.

The registry is the step that recognizes them as one: each part of a chunk
is assigned to a topic of its room, and a topic that recurs gets a name and
a short explanation. The room's page then shows what the room keeps coming
back to and how much of it that is, and the same list is rendered as the
block a prompt would hold in place of every chunk.

## What a topic is

A part belongs to two topics, one on each axis:

| axis | the question a part and a topic's parts are compared on | what recurs |
|---|---|---|
| `task` | do the two requests ask for the same kind of task | computing square roots, looking up a date in the diary |
| `thing` | are the two requests about the same named thing | a project, a person, a tool |

A topic with one member is a one-off and is not shown. It is kept: it is
what the next part about the same thing joins.

## Assignment

One job per chunk (`room_topics`, subject: the chunk's uuid), queued when the
chunk is stored and, for chunks stored before, with the room's chunk jobs.
For each part, in order:

1. The part's phrase (`summary`, what was wanted) is embedded.
2. On each axis the room's topics are ranked by the cosine between that
   vector and their exemplars' (`EXEMPLARS` = the first 2 members), and the
   nearest `CANDIDATES` = 5 are kept. Without a vector the newest 5 are.
3. Each exemplar of a candidate is put to the decision model with the part,
   one request for both axes' questions. A topic's score is the mean over
   its exemplars.
4. The part joins the best topic at or above `JOIN_AT` = 0.5, else it
   starts a topic of its own, named after the part's first topic name.

About six requests of a third of a second per part. They are not counted
toward the machine's rest: no text is generated.

Measured on one room's 56 parts, with every pair of parts scored once
(1,540 requests) and the assignment replayed from those scores: scoring a
part against every topic, against the nearest 5 and against the nearest 8
gave the same recurring topics but one, and 2 exemplars gave the same as 3.
On the `task` axis 12 topics recurred and held 56% of the messages; on
`thing`, 9 and 37%. Two of the 21 were wrong joins, both of "who is" with
"who are you".

The decision model is the one bound to `assistant.topic_match`; unbound,
the one bound to `assistant.repeat_check`. With neither, the job is skipped.

A job that a new message stops has written the parts it finished; run
again it goes on with the rest.

## Name and explanation

A topic that reaches 2 members, and again each time it has doubled, is
explained by one generative call (the slot `assistant.topic_namer`, local
models only; unbound, `assistant.default`): given its members' requests and
results, it writes a name and an explanation of at most eight words. On `task` the
explanation says what the user is doing with these requests; on `thing`,
what the thing is.

The model sees what was asked and not why. Asked about five square roots,
it says that roots are asked for, and not that the user is testing whether
the assistant can run a program. So the operator can edit both fields on
the room's page, and an edited topic is never written by the model again.

## What a topic was answered with

The requests do not say why they recur, and the explaining call cannot
either. The records can say what they exercise. Each part keeps the tools
its replies used, read off the runs' steps (`room_chunks.reply_tools`), so
a topic's row says "answered with: program 5/5, memory query 2/5": a tool
counts for a part where it ran, out of the topic's parts, and a program
that only failed is counted apart. The block carries it as `answered_with`.

The explaining call is given each request's tools too. A sentence telling
it to name the tool was tried and dropped: it named the program in 2 of 8
explanations of five square roots that a program had computed.

## What is stored

`room_topic`: `room_uuid`, `axis`, `name`, `explanation`,
`operator_edited`, `explained_members` (how many members it had when the
model last explained it).

`room_topic_member`: `topic_uuid`, `room_uuid`, `axis`, `chunk_uuid`,
`part_index`, `phrase`, `messages` (the part's length), `p` (the score it
joined on; NULL for the part that started the topic), `embedding`. One row
per `(chunk_uuid, part_index, axis)`.

A topic belongs to its room and not to a series of chunks. A new prompt
version summarizes the room again; the new chunks' parts are assigned to
the topics there are, so the names and the operator's explanations carry
over. A page counts a topic's members in the series it shows.

## The page

The room's page gets a third view, `topics`: per axis a table of the
recurring topics with their share of the series' messages, the number of
parts, the name and the explanation, and the members folded under each. A
click on a name opens a modal with the name and the explanation
(`notes/ui-modal-rename.md`). Under the tables, the block as a prompt would
hold it, with its size against the original messages:

```
<room_topics authority="context" content_is_data="true">
  <topic kind="task" share="8%" asked="5" name="…" about="…" answered_with="program 5/5"/>
  <topic kind="thing" share="6%" asked="3" name="…" about="…"/>
  <other share="44%"/>
</room_topics>
```

`other` is what no recurring `task` topic holds. No prompt holds the block
yet.

## Not in this step

- A registry across rooms: the same assignment, run over the rooms' topics.
- Merging two topics, or moving a part to another topic, by hand.
- Putting the block into the assistant's prompt.
