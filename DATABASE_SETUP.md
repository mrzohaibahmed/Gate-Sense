# Database setup (SQL Server)

GateSense keeps its attendance records, people, users and audit log in Microsoft SQL Server. Face embeddings stay in
`data\gallery.npz`. Follow these steps once on each new computer, before the first `start.bat`. You need
administrator rights on the computer.

The examples use the instance name `SQLEXPRESS` and the project folder `C:\GateSense`. Replace them with your own.

## 1. Install the software

1. **SQL Server Express**, from https://www.microsoft.com/sql-server/sql-server-downloads.
   - Choose **Custom**, not Basic, so you can select **Mixed Mode** (SQL Server and Windows authentication) and set an
     `sa` password.
   - Write down the **instance name**. A fresh install is usually `SQLEXPRESS`. If another instance already exists, it
     may be `SQLEXPRESS01` or similar.
2. **ODBC Driver 18 for SQL Server (x64)**, from Microsoft's download page. GateSense can't reach SQL Server without it.
3. **sqlcmd**, to run the commands below: `winget install sqlcmd`. SQL Server Management Studio (SSMS) works too.

## 2. Allow SQL Server logins

Skip this step if you chose Mixed Mode during the install.

In SSMS, right-click the instance > Properties > Security > "SQL Server and Windows Authentication mode". Then restart
the service, in PowerShell as administrator:

```powershell
Restart-Service 'MSSQL$SQLEXPRESS'
```

## 3. Create the database and GateSense's login

Connect with your Windows account:

```powershell
sqlcmd -S localhost\SQLEXPRESS -E -C
```

Run the following. Choose a strong password; it must meet the Windows password policy.

```sql
CREATE DATABASE GateSense;
ALTER DATABASE GateSense SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;  -- readers never wait for writers
CREATE LOGIN gatesense_app WITH PASSWORD = N'choose-a-strong-password', DEFAULT_DATABASE = GateSense, CHECK_POLICY = ON;
USE GateSense;
CREATE USER gatesense_app FOR LOGIN gatesense_app;
ALTER ROLE db_datareader ADD MEMBER gatesense_app;
ALTER ROLE db_datawriter ADD MEMBER gatesense_app;
ALTER ROLE db_ddladmin ADD MEMBER gatesense_app;        -- creates GateSense's tables (first start)
ALTER ROLE db_backupoperator ADD MEMBER gatesense_app;  -- the nightly and "Back up now" backups
USE master;
CREATE USER gatesense_app FOR LOGIN gatesense_app;
GRANT CREATE DATABASE TO gatesense_app;                 -- RESTORE VERIFYONLY (checking each backup) requires it
GO
```

Type `exit` to leave sqlcmd. Don't create any tables: GateSense creates them, with the default site and shifts, the
first time it connects.

## 4. Let SQL Server write the backup folder

The SQL Server service writes the backup files itself, so it needs write access to `data\backups`:

```powershell
mkdir "C:\GateSense\data\backups"
icacls "C:\GateSense\data\backups" /grant "NT Service\MSSQL`$SQLEXPRESS:(OI)(CI)M"
```

In PowerShell, the backtick before `$` is required. In cmd, leave it out.

## 5. Point GateSense at the database

Copy `.env.example` to `.env` in the project folder if you haven't already, and set this line. Keep the single quotes.

```
GATESENSE_DB_URL='mssql+pyodbc://gatesense_app:PASSWORD@localhost\SQLEXPRESS/GateSense?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes'
```

If the password contains any of these characters, write them percent-encoded:

| Character | Write | Character | Write |
|---|---|---|---|
| `@` | `%40` | `&` | `%26` |
| `:` | `%3A` | `%` | `%25` |
| `/` | `%2F` | `+` | `%2B` |
| `?` | `%3F` | space | `%20` |
| `#` | `%23` | | |

A `GATESENSE_DB_URL` environment variable (set with `setx`) takes priority over `.env`. If one is set from an
earlier setup, remove it:

```powershell
[Environment]::SetEnvironmentVariable('GATESENSE_DB_URL', $null, 'User')
```

Then open a new terminal. GateSense installed as a Windows service (`scripts\install_service.ps1`) reads the same
`.env`.

## 6. Check it

```powershell
.venv\Scripts\python -m api.run --preflight
```

Or double-click `start.bat`. If GateSense can't use the database, it says why: a wrong password, a wrong instance
name, or a missing ODBC driver. The password is hidden in the message.

A warning that READ_COMMITTED_SNAPSHOT is off means the `ALTER DATABASE` line in step 3 didn't run. Run it again
while GateSense is stopped.

## Moving existing data from another computer

1. On the old computer: Settings > System > **Back up now**. This writes a backup set (the `.bak` file and the other
   parts, all with the same time in their names) to `data\backups\`.
2. On the new computer, do steps 1 to 5 above, but **don't start GateSense yet**. The empty database from step 3 is
   needed: the restore checks the backup through GateSense's login, and then replaces that database.
3. Copy the backup set's files into the new computer's `data\backups\`. Do this after step 4, so the SQL Server
   service can read them.
4. Run:

   ```powershell
   .venv\Scripts\python scripts\restore.py latest
   ```

   It checks the `.bak` file and prints the `RESTORE DATABASE` commands. Run them in sqlcmd with your Windows account
   (`sqlcmd -S localhost\SQLEXPRESS -E -C`); GateSense's own login can't replace its database. Then type `GO`.

   If the restore fails with "Directory lookup for the file ... failed", the old computer kept the database files in
   another folder. List the backup's files and add a `MOVE` for each to the new instance's `DATA` folder:

   ```sql
   RESTORE FILELISTONLY FROM DISK = N'C:\GateSense\data\backups\<the .bak file>';
   GO
   RESTORE DATABASE [GateSense] FROM DISK = N'C:\GateSense\data\backups\<the .bak file>' WITH REPLACE, CHECKSUM,
       MOVE N'GateSense' TO N'C:\Program Files\Microsoft SQL Server\<instance folder>\MSSQL\DATA\GateSense.mdf',
       MOVE N'GateSense_log' TO N'C:\Program Files\Microsoft SQL Server\<instance folder>\MSSQL\DATA\GateSense_log.ldf';
   ALTER DATABASE [GateSense] SET MULTI_USER;
   GO
   ```

   Use the logical names from the `LogicalName` column if they differ from `GateSense` and `GateSense_log`.
5. The restored database still links its `gatesense_app` user to the old computer's login. Link it to the new one:

   ```sql
   USE GateSense;
   ALTER USER gatesense_app WITH LOGIN = gatesense_app;
   GO
   ```

6. Run the command `restore.py` printed for the other parts of the set: the gallery, the enrollment photos
   (`known\`), `cameras.json` and `settings.json`. It looks like
   `.venv\Scripts\python scripts\restore.py <time> --only gallery,known,cameras,settings`.
7. Start GateSense with `start.bat` and sign in with the old computer's accounts.

## SQL Server on a different computer

If SQL Server runs on another computer from GateSense, do the following on the SQL Server computer:

- In SQL Server Configuration Manager > SQL Server Network Configuration, enable **TCP/IP**, then restart the service.
- Start the **SQL Server Browser** service and set it to Automatic. A named instance such as `SQLEXPRESS` needs it.
- Open Windows Firewall for the instance's TCP port (1433 by default) and UDP 1434 for the Browser.

In the URL, use that computer's name instead of `localhost`, for example `@sqlpc\SQLEXPRESS/GateSense`.

The backup folder must be one the SQL Server computer's service can write to. Use a shared folder that both computers
can reach, and grant the service write access to it as in step 4.

## Troubleshooting

| Message | Cause |
|---|---|
| `GATESENSE_DB_URL is not set` | No line in `.env` (step 5), or `.env` isn't in the project folder. |
| `Login failed for user 'gatesense_app'` | Wrong password in the URL, an unencoded special character, or Mixed Mode is off (step 2). |
| `Data source name not found` / `Can't open lib` | ODBC Driver 18 isn't installed, or the `driver=` part of the URL is mistyped. |
| `server was not found or was not accessible` | Wrong instance name, the SQL Server service is stopped, or (another computer) TCP/IP, Browser or firewall (see above). |
| A backup fails with "access denied" | The service can't write `data\backups` (step 4). |
