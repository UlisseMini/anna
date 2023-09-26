from fastapi import FastAPI, Form, Request, HTTPException, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import sqlite3
import stripe
import subprocess
import os
import json
import httpx
import time
import asyncio
from Levenshtein import distance

# run source ../.env to get path variables
from dotenv import load_dotenv
load_dotenv('../.env')


OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
stripe.api_key = os.environ["STRIPE_API_KEY"]
HOST = os.environ["HOST"]

app = FastAPI()

# break time measures the epoch time at which the break will end. If it's in the future, then we are on break.
break_time = 0
def start_break(minutes):
    global break_time
    break_time = time.time() + minutes * 60
    print('break started')
    print(f'break lasts {minutes} minutes')



def setup_db(conn):
    with conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id TEXT UNIQUE, -- globally unique machine id
                version TEXT, -- version of the frontend app
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- the user the message is associated with. linked to users.id via foreign key
                user_id INTEGER,

                -- the message, in openai format. if role == "user", then this is the user's message,
                -- if role == "assistant" then this is the assistant's response to a previous message.
                role TEXT,
                content TEXT,

                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,

                app TEXT,
                window_title TEXT,
                time INTEGER NOT NULL, -- epoch time, from swift
                -- TODO add more fields for tracking activity

                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,

            timesinks TEXT,
            endorsed_activities TEXT,

            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """)


@app.on_event("startup")
def startup():
    # setup db
    app.state.db = sqlite3.connect("db.sqlite3")
    setup_db(app.state.db)


@app.on_event("shutdown")
def shutdown():
    app.state.db.close()


@app.post("/create-checkout-session")
def create_checkout_session(lookup_key: str = Form(...)):
    try:
        print(lookup_key)
        prices = stripe.Price.list(
            lookup_keys=[lookup_key],
            expand=["data.product"]
        )
        print(prices)
        checkout_session = stripe.checkout.Session.create(
            line_items=[
                {
                    "price": prices.data[0].id,
                    "quantity": 1,
                }
            ],
            mode='subscription',
            success_url=f"{HOST}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{HOST}/cancel.html",
        )
        return RedirectResponse(url=checkout_session.url, status_code=303)
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500)



@app.post("/create-portal-session")
def create_portal_session(session_id: str = Form(...)):
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
        portalSession = stripe.billing_portal.Session.create(
            customer=checkout_session.customer,
            return_url=HOST,
        )
        return RedirectResponse(url=portalSession.url, status_code=303)
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500)



@app.post("/webhook")
async def webhook_received(request: Request):
    webhook_secret = 'whsec_12345'
    request_data = await request.json()
    body = await request.body()

    if webhook_secret:
        signature = request.headers.get('stripe-signature')
        if not signature:
            return HTTPException(status_code=400, detail="Missing stripe-signature header")

        try:
            event = stripe.Webhook.construct_event(
                payload=body, sig_header=signature, secret=webhook_secret)
            data = event['data']
        except Exception as e:
            return e

        event_type = event['type']
    else:
        data = request_data['data']
        event_type = request_data['type']

    print('stripe event ' + event_type)

    # Handle different event types here
    # FIXME: Not handling these is literally illegal (impossible to unsubscribe)
    raise HTTPException(status_code=500, detail="Server error: Not implemented")

    return {"status": "success"}



client = httpx.AsyncClient(
    base_url="https://api.openai.com",
    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
    timeout=100,
)


# Respond to trigger of time sink
ON_TRIGGER_PROMPT = """
You are a helpful assistant that helps the user manage their time.

The user's common time sinks are:
{timesinks}

---

And the user's common endorsed activities are:
{endorsed_activities}

---

When an activity report indicates the user might be distracted by a time sink, ask them questions about what they're doing and empathetically guide them towards using their time in a way they endorse, or taking a break. Write messages in a short (~25 words) encouraging and empathetic texting style. If taking a break, pause check-ins using the break function for the duration of the break. When the next activity log shows, the break has ended.

If the user is not on a time sink, respond with an empty message to avoid interrupting the user.

Example:
Assistant: Why are you on YouTube right now? Is there anything you'd rather be doing?
User: I'm tired because I didn't sleep well.
Assistant: I'm sorry to hear that. Would you like to take a break?
""".strip()


# call the trigger function with "trigger": true or "trigger": false
TRIGGER_PROMPT = """
If the user is on a timesink, then trigger the app. Otherwise, pass false to do nothing.
The user's common time sinks are:
{timesinks}

The user's recent activity has been:
{activity}

If the user has been on a time sink for over a minute trigger the app, otherwise pass false to do nothing.
""".strip()

# Get version from most recent git tag
GIT_DIR = '.git' if os.path.isdir('.git') else '../.git' if os.path.isdir('../.git') else None
if not GIT_DIR: raise Exception('No .git directory found')
VERSION = subprocess.check_output(["git", "describe", "--tags"]).strip().decode("utf-8")
RELEASE_MSG = subprocess.check_output(["git", "log", "-1", "--pretty=%B", VERSION]).strip().decode("utf-8")

INITIAL_MESSAGE = f"""
Welcome to BossGPT! Server {VERSION}, App {{APP_VERSION}}. Latest server update: {RELEASE_MSG}
""".strip()


# function to determine whether to trigger the app or not
async def should_trigger(prompt: str) -> bool:
    # check if break is in progress
    if break_time > time.time():
        print('break in progress')
        return False
    print('trigger prompt:\n', prompt)
    messages = [{'role': 'user', 'content': prompt}]
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-3.5-turbo",
            "messages": messages,
            "functions": [
                {
                    # this should be either True or False, always called.
                    "name": "trigger",
                    "description": "Pass true to trigger an intervention, or false to do nothing",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "trigger": {
                                "type": "boolean"
                            }
                        },
                        "required": ["trigger"],
                    },
                }
            ],
            "max_tokens": 100,
            "temperature": 0,
        }
    )
    resp.raise_for_status()
    resp_data = resp.json()
    message = resp_data['choices'][0]['message']
    # trigger iff the message is a function call to trigger
    trigger = False
    if message.get("function_call") and message["function_call"]["name"] == 'trigger':
        print(message["function_call"])
        # TypeError: string indices must be integers
        try:
            arguments = json.loads(message["function_call"]["arguments"])
            print(arguments)
            print(arguments["trigger"])
            trigger = arguments["trigger"]
        except json.JSONDecodeError:
            pass
    else:
        print('WARNING: no trigger call')
    return trigger


class WebSocketHandler():
    """
    Web socket handler, one per connection. Handles
    * Keeping client, server, and database in sync
    * Querying GPT for triggers and sending messages when required
    """

    def __init__(self, ws, db):
        self.ws = ws
        self.db = db
        self.user_id = None
        self.version = None


    # TODO: Consistent names for db functions

    def get_messages(self, limit=100):
        "Get the most recent n messages in reverse-chronological order"
        c = self.db.cursor()
        c.execute("SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?", (self.user_id, limit))
        return [{"role": role, "content": content} for role, content in reversed(c.fetchall())]


    def record_msg(self, msg):
        self.db.execute("INSERT INTO messages (user_id, content, role) VALUES (?, ?, ?)", (self.user_id, msg['content'], msg['role']))
        self.db.commit()

    def record_activity(self, data):
        c = self.db.cursor()
        c.execute("INSERT INTO activity (user_id, app, window_title, time) VALUES (?, ?, ?, ?)", (self.user_id, data['app'], data['window_title'], data['time']))
        self.db.commit()


    async def send_msg(self, msg):
        await self.ws.send_json(msg)


    async def send_and_record_msg(self, msg):
        self.record_msg(msg)
        await self.send_msg(msg)


    async def on_register(self, data):
        print(f"registering user {data}")
        user = data['user']
        # plop into database if not already there
        c = self.db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO users (machine_id) VALUES (?)",
            (user['machine_id'],)
        )
        self.user_id, = c.execute("SELECT id FROM users WHERE machine_id = ?", (user['machine_id'],)).fetchone()
        assert self.user_id is not None

        # update version
        c.execute("UPDATE users SET version = ? WHERE id = ?", (data.get('version'), self.user_id))
        self.version = data.get('version')

        # add empty settings row if it doesn't exist
        settings = None
        try:
            settings = await self.get_settings()
            # send settings to client
            await self.send_msg({"type": "settings", **settings})
        except KeyError:
            c.execute("INSERT INTO settings (user_id) VALUES (?)", (self.user_id,))
            self.db.commit()

        # fetch 100 most recent messages & shove into client
        for msg in self.get_messages(limit=100):
            await self.send_msg({"type": "msg", **msg})

        print(f"done registering {data} user id {self.user_id}")

        await self.send_msg({"type": "msg", "role": "special", "content": INITIAL_MESSAGE.format(APP_VERSION=self.version)})


    async def receive(self, timeout=10):
        try:
            text = await asyncio.wait_for(self.ws.receive_text(), timeout=timeout)
            return json.loads(text)
        except asyncio.TimeoutError:
            return None


    @staticmethod
    def add_activity_dur(activities, now=None):
        now = now or time.time()
        # prevent IndexErrors
        if len(activities) == 0:
            return activities
        # add time spent on each app by subtracting the time of the next app from the time of the current app
        for i in range(len(activities) - 1):
            activities[i]['dur'] = round(activities[i]['time'] - activities[i+1]['time'])
        # add time spent on last app by subtracting the time of the last app from now
        activities[-1]['dur'] = round(now - activities[-1]['time'])
        return activities

    @staticmethod
    def merge_activities(activities, k=5):
        """
        merge activities with similar titles, that is, distance(s1, s2) < k
        keep the description of the first activity in the list.
        passing a list in reverse chronological order is recommended,
        as then the most recent description will be kept.
        """
        for i in range(len(activities)):
            for j in range(i+1, len(activities)):
                a1, a2 = activities[i], activities[j]
                if a1 is None or a2 is None: continue

                dist = distance(a1['window_title'], a2['window_title'])
                if dist < k:
                    # print(f'merge {a2} into {a1} dist {dist}')
                    a1['dur'] += a2['dur'] # merge a2 into a1
                    activities[j] = None   # mark a2 to be deleted

        # delete the marked activities
        return [a for a in activities if a is not None]


    @staticmethod
    def get_activities_for_report(db, user_id: int, last_n_seconds: int, max_n: int, now=None):
        # get the activites from user in the last 10 minutes (n secnods)
        now = now if now is not None else time.time()
        c = db.cursor()
        rows = c.execute(
            f"SELECT app, window_title, time FROM activity WHERE user_id = ? AND time > ? AND time < ? ORDER BY time DESC LIMIT ?",
            (user_id, now - last_n_seconds, now, max_n)
        ).fetchall()
        if len(rows) == 0:
            # just put most recent activity
            rows = c.execute(f"SELECT app, window_title, time FROM activity WHERE user_id = ? AND time < ? ORDER BY time DESC LIMIT 1", (user_id, now)).fetchall()
        activities = [{"app": app, "window_title": window_title, "time": time} for app, window_title, time in rows]
        WebSocketHandler.add_activity_dur(activities, now=now)

        # merge activities with similar titles
        activities = WebSocketHandler.merge_activities(activities)

        # remove very short activities  (less than 10 seconds)
        activities = [a for a in activities if a['dur'] > 10]

        # make most recent activity last
        activities.reverse()
        return activities


    @staticmethod
    def get_activity_report(activities) -> str:
        activity = '\n'.join(f"{a['dur']//60:.0f}m {a['dur']%60:.0f}s on {a['app']} - {a['window_title']}" for a in activities)
        return activity




    async def check_in(self, max_n=100, last_n_seconds=600):
        print('checking in...')
        assert self.user_id, f'no user id for {self}'

        timesinks: str = (await self.get_settings()).get('timesinks') or ''
        if timesinks.strip() == '':
            print('No time sinks configured -- skipping check in')
            return

        activities = self.get_activities_for_report(self.db, self.user_id, last_n_seconds=last_n_seconds, max_n=max_n)
        activity = self.get_activity_report(activities)

        trigger = await should_trigger(TRIGGER_PROMPT.format(minutes=last_n_seconds//60, timesinks=timesinks, activity=activity))
        if trigger:
            # add the activity report message to the message history, and then call the normal response function
            await self.send_and_record_msg({"type": "msg", "role": "user", "content": f"Activity report:\n{activity}"})
            await self.respond_to_msg()


    async def update_settings(self, settings_msg):
        print('updating settings', settings_msg)
        # insert timesinks data into database. we don't delete anything.
        timesinks: str = settings_msg["timesinks"]
        endorsed_activities: str = settings_msg["endorsed_activities"]
        c = self.db.cursor()
        c.execute("INSERT INTO settings (user_id, timesinks, endorsed_activities) VALUES (?, ?, ?)", (self.user_id, timesinks, endorsed_activities))
        self.db.commit()


    async def get_settings(self):
        assert self.user_id is not None, "user must be registered before getting settings"
        # fetch most recent settings from that user id
        c = self.db.cursor()
        c.execute("SELECT timesinks, endorsed_activities FROM settings WHERE user_id = ? ORDER BY id DESC LIMIT 1", (self.user_id,))
        settings = c.fetchone()
        if not settings:
            raise KeyError(f"user {self.user_id} doesn't have settings yet")
        return {"timesinks": settings[0], "endorsed_activities": settings[1]}


    async def respond_to_msg(self):
        settings = await self.get_settings()
        timesinks = settings['timesinks']
        endorsed_activities = settings['endorsed_activities']
        sys_prompt = ON_TRIGGER_PROMPT.format(timesinks=timesinks, endorsed_activities=endorsed_activities)
        # add the system message and the most recent 20 messages in the message history
        messages = [{'role': 'system', 'content': sys_prompt}] + self.get_messages(limit=20)
        functions = [
            {
                "name": "start_break",
                "description": "Start a break for the given number of minutes",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutes": {
                            "type": "integer",
                            "description": "The number of minutes to take a break for",
                        }
                    },
                    "required": ["minutes"],
                },
            }
        ]

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": messages, "functions": functions, "max_tokens": 1000}
        )
        resp.raise_for_status()
        resp_json = resp.json()
        message = resp_json['choices'][0]['message']

        if message.get("function_call"):
            # Step 3: call the function
            # Note: the JSON response may not always be valid; be sure to handle errors
            available_functions = {
                "start_break": start_break,
            }  # only one function in this example, but you can have multiple
            function_name = message["function_call"]["name"]
            function_to_call = available_functions[function_name]
            function_args = json.loads(message["function_call"]["arguments"])
            function_to_call(
                minutes=function_args.get("minutes"),
            )
        elif message["content"].strip() != "":
            await self.send_and_record_msg({"type": "msg", **message, 'notifOpts': ['sound', 'alert']})


    async def run(self):
        await self.ws.accept()

        # get registration
        data = await self.ws.receive_json()
        if data['type'] == 'register':
            await self.on_register(data)
        else:
            raise ValueError(f"message type {data['type']} disallowed for first message")

        check_in_interval = 300
        last_check_in = time.time()

        while True:
            data = await self.receive(timeout=10)
            if not data:
                if time.time() - last_check_in > check_in_interval:
                    last_check_in = time.time()
                    await self.check_in(last_n_seconds=check_in_interval)
                    print('check in')

                continue

            if data['type'] == 'activity_info':
                print('activity', data)
                self.record_activity(data)
            elif data['type'] == 'msg':
                self.record_msg(data)
                await self.respond_to_msg()
            elif data['type'] == 'settings':
                await self.update_settings(data)
            elif data['type'] == 'debug':
                cmd = data.get('cmd')
                if cmd == 'checkin':
                    await self.check_in()
            else:
                raise ValueError(f"Unknown message type: {data['type']}")



@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    handler = WebSocketHandler(websocket, app.state.db)
    await handler.run()


app.mount("/", StaticFiles(directory="static", html=True))
