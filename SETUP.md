# Ballot Return Finder — Setup Instructions

Follow these steps exactly. You do not need to understand what any of it means.
Estimated time: 20 minutes.

---

## STEP 1 — Create a GitHub account (if you don't have one)

1. Go to https://github.com
2. Click "Sign up"
3. Use your email and create a password
4. Verify your email

---

## STEP 2 — Create a new repository on GitHub

1. Once logged in, click the **+** icon in the top right corner
2. Click **New repository**
3. Name it: `ballot-finder`
4. Leave everything else as-is
5. Click **Create repository**
6. You will see a page with setup instructions — leave it open

---

## STEP 3 — Upload your files to GitHub

1. On the repository page, click **uploading an existing file** (it's a link in the middle of the page)
2. Drag ALL of these files into the upload area:
   - `app.py`
   - `requirements.txt`
   - `render.yaml`
   - The entire `static` folder (drag the folder itself)
3. Scroll down and click **Commit changes**

Your files are now on GitHub.

---

## STEP 4 — Create a Render account

1. Go to https://render.com
2. Click **Get Started for Free**
3. Sign up with your GitHub account (click "Sign up with GitHub")
4. Authorize Render to access your GitHub

---

## STEP 5 — Deploy to Render

1. Once logged into Render, click **New** in the top right
2. Click **Web Service**
3. Click **Connect** next to your `ballot-finder` repository
4. Render will detect the settings automatically from `render.yaml`
5. Scroll down to **Environment Variables**
6. Click **Add Environment Variable**
   - Key: `ADMIN_PASSWORD`
   - Value: choose a password you will remember (e.g. `Denver2026!`)
7. Click **Create Web Service**
8. Wait 3-5 minutes while Render builds and deploys the app
9. When it says **Live**, your app is running

Your URL will be something like: `https://ballot-finder.onrender.com`

---

## STEP 6 — Upgrade to paid tier (recommended for 100 users)

The free tier "sleeps" after 15 minutes of inactivity, which means
the first person to open it each morning will wait 30 seconds for it to wake up.

To fix this:
1. In Render, click your service
2. Click **Settings**
3. Under **Instance Type**, select **Starter** ($7/month)
4. Click **Save Changes**

This keeps the server running 24/7.

---

## STEP 7 — Load your first data file

1. Go to: `https://your-app-name.onrender.com/admin`
2. Enter the admin password you set in Step 5
3. Tap the file area and select the CE-068 file you downloaded from denvergov.org
4. Click **Upload & Refresh Data**
5. Wait about 30 seconds for it to parse the file
6. You will see a success message with voter counts

The app will then start geocoding addresses in the background.
This takes several hours the first time (Nominatim allows 1 request/second).
The map gets more accurate as geocoding completes.
After the first run, geocodes are saved permanently — future uploads are instant.

---

## DAILY ROUTINE (takes about 2 minutes each morning)

1. Go to denvergov.org → Elections → Data & Maps → download CE-068
2. Go to `https://your-app-name.onrender.com/admin`
3. Enter password, select the new file, click Upload
4. Done — volunteers see fresh data immediately

---

## SHARE WITH VOLUNTEERS

Send volunteers this URL: `https://your-app-name.onrender.com`

That's all they need. No login, no file upload, no instructions.
They open it on their phone, type their address, and see the map.

---

## TROUBLESHOOTING

**"No data loaded yet"** — You need to upload a file at /admin first.

**App is slow to load** — You are on the free tier. Upgrade to Starter ($7/mo) in Step 6.

**Wrong password** — The password is the one you set as ADMIN_PASSWORD in Step 5.
  To change it: in Render, go to your service → Environment → edit ADMIN_PASSWORD → Save.
  The service will restart automatically.

**Map shows no pins after search** — Geocoding is still running in the background.
  Check /admin to see the geocoding progress. Try again in a few hours.

**Need to update the app code** — Edit the files on GitHub. Render redeploys automatically.

---

## SECURITY NOTES

- Volunteers can only search — they cannot upload data or change anything
- The /admin page requires a password
- All voter data stays on your Render server — nothing is stored in volunteers' browsers
- The data is public record, but it is good practice to share the URL only with your team
