# Run the Fasalrin panel on Oracle Cloud (free, India)

The panel and scripts run on a free Oracle Cloud machine in Mumbai or Hyderabad, so your laptop can stay off.
You connect to that machine's desktop with **Remote Desktop**. The panel and the Chrome window the scripts open
are on that desktop, and you log in to fasalrin.gov.in there just like on the branch PC.

Only your own devices can reach the machine. Remote Desktop works only over **Tailscale**, and no port is opened
to the internet except SSH, which needs your key.

> Run the work in **one place only**. Pick the PC or the server for a branch. Two copies of the progress files
> would drift apart, and the same row could be filed twice.

---

## 1. Create the Oracle account (once)

1. Go to <https://signup.cloud.oracle.com>. Sign up with **Home Region = India West (Mumbai)** or
   **India South (Hyderabad)**. The home region cannot be changed later.
2. A credit or debit card is needed to verify the account. Always Free resources are not charged.
   Do not click "Upgrade to paid".

## 2. Create the machine

Menu → **Compute → Instances → Create instance**:

| Setting | Choose |
|---|---|
| Name | `fasalrin` |
| Image | **Canonical Ubuntu 24.04** (the aarch64 one is picked with the shape below) |
| Shape | **Ampere → VM.Standard.A1.Flex**, **2 OCPU, 12 GB memory** (Always Free eligible) |
| Networking | default (new VCN, public subnet, assign a public IPv4 address) |
| SSH keys | **Generate a key pair for me**, then **download the private key** and keep it safe |
| Boot volume | default (≈ 47 GB, free) |

Press **Create**. If you see *"Out of capacity"*, try another availability domain (AD-1/2/3), or try again
later. Capacity in India often frees up within hours to days.

Note the **Public IP address** shown on the instance page.

## 3. Connect over SSH (from your Windows laptop)

Open PowerShell. Put the downloaded key in your user folder and run:

```bash
ssh -i $HOME\ssh-key-fasalrin.key ubuntu@PUBLIC_IP
```

(If Windows says the key's permissions are too open, right-click the key → Properties → Security, and leave
only your own user.)

## 4. Get the code and run the setup (on the server)

Your GitHub repo is private, so sign in to GitHub on the server first. GitHub CLI shows a code that you enter
in your laptop's browser:

```bash
sudo apt-get update -y && sudo apt-get install -y gh
gh auth login --hostname github.com --git-protocol https --web
gh repo clone sohil2312/fasalrin-is-regular
cd fasalrin-is-regular
bash deploy/oracle/setup.sh
```

The setup takes about 10–15 minutes. Then do the three things it prints at the end:

```bash
sudo tailscale up
```
Open the link it prints and sign in with the **same Tailscale account** as your laptop and phone.

```bash
sudo passwd ubuntu
```
Choose a strong password. You type it when you open Remote Desktop.

## 5. Install Tailscale on your laptop / phone

Install it from <https://tailscale.com/download> and sign in with the same account. In the Tailscale app you
will see the server, named `fasalrin`, with an address like `100.x.y.z`.

## 6. Open the desktop

* **Windows laptop:** Start → **Remote Desktop Connection** → computer `fasalrin` (or its `100.x` address)
  → user `ubuntu` and the password from step 4.
* **Android / iPhone:** the **Windows App** (Microsoft Remote Desktop), same address and user.

On the desktop, double-click **Fasalrin panel**. The panel opens in Firefox at `http://localhost:8765`.
When a job starts, its Chrome window opens on the same desktop. Log in there with the Branch User or
Branch Head login.

**You can close Remote Desktop while a job runs.** The session keeps running, and reconnecting brings you back
to the same desktop. Don't choose *Log out*, because that ends the session and the job.

## 7. Move your data (once, from the laptop)

The work lists, masters and reports are not in GitHub on purpose (Aadhaar data). Copy them over Tailscale.
**Stop all jobs on the PC first.** In PowerShell, in the project folder:

```bash
scp -i $HOME\ssh-key-fasalrin.key -r branches master ubuntu@fasalrin:~/fasalrin-is-regular/
```

Don't copy the `.pw_profile*` folders. Log in fresh on the server instead.

Instead of copying, you can upload the master in the panel and build the branches again. The progress already
made is only known from the copied `branches` folder or from a matched portal report.

## Updating to a new version

On the server (SSH or a terminal on the desktop), with no job running:

```bash
cd ~/fasalrin-is-regular
git pull
.venv/bin/pip install -r requirements.txt
```

Then close the panel window and start **Fasalrin panel** again.

## Backups

Oracle can reclaim Always Free machines that look idle for 7 days. Copy the data back now and then, from the
laptop:

```bash
scp -i $HOME\ssh-key-fasalrin.key -r ubuntu@fasalrin:~/fasalrin-is-regular/branches ./backup_branches
```

## Good to know

* **Region:** the portal sees the server's Indian IP. That is why the machine must be in Mumbai or Hyderabad.
* **Data:** the farmer data sits on the server's disk in India. Check with your bank that this is allowed.
* **Security:** never open port 3389 (Remote Desktop) in Oracle's Security List. Tailscale makes it unnecessary.
