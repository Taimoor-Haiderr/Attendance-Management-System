# 📋 Attendance Management System

A modern **Python-based Attendance Management System** built with **Tkinter GUI**, **SQLite database**, and **CSV synchronization**.
This application provides an efficient and user-friendly way to manage attendance records with features like attendance marking, record viewing, editing, report generation, backups, and email sharing.

---

##  Features

###  Attendance Management

* Mark attendance with:

  * **Present**
  * **Absent**
  * **Late**
* Duplicate attendance prevention using **SQLite UNIQUE constraints**
* Optional notes for attendance entries
* Automatic person registration/update

### Person Management

* Stores:

  * ID
  * Full Name
  * Department
* Automatic **upsert (insert/update)** support

###  Record Viewing & Search

Filter attendance records using:

* Person ID
* Name
* Date
* Month
* Attendance Status

###  Update & Delete Records

* Edit attendance status and notes
* Delete unwanted records
* Popup dialogs with confirmation prompts

###  Attendance Reports

Generate attendance summaries including:

* Present count
* Absent count
* Late count
* Total attendance
* Attendance percentage

Supports:

* Monthly reports
* Individual reports
* All-time reports

###  CSV Export & Synchronization

* Sync database records to CSV
* Export attendance reports
* Import attendance from CSV files

###  Backup System

Manual backup tools for:

* CSV files
* SQLite database
* Combined backup

Backup history viewer included.

###  Email Integration

Send reports directly via email using:

* SMTP server configuration
* File attachments
* Multi-threaded email sending

---

#  Technologies Used

| Technology                 | Purpose                   |
| -------------------------- | ------------------------- |
| **Python**                 | Core programming language |
| **Tkinter**                | GUI development           |
| **SQLite3**                | Database storage          |
| **CSV Module**             | CSV export/import         |
| **Matplotlib**             | Data visualization        |
| **Threading**              | Background email sending  |
| **SMTP / Email Libraries** | Email reports             |
| **OS & Shutil**            | File handling & backups   |
| **Regex (re)**             | Input validation          |
| **Datetime**               | Date and time management  |



#  Application Modules

The system contains **5 main modules**:

1. **Mark Attendance**
2. **View Records**
3. **Update Records**
4. **Generate Reports**
5. **Backup & Tools**

---

#  Data Safety Features

* Duplicate entry prevention
* Backup system
* CSV synchronization
* Database persistence
* Validation for:

  * IDs
  * Dates
  * Inputs



#  Author

**TAIMOOR HAIDER*

Built using **Python + Tkinter + SQLite** for efficient attendance management.


You can copy this directly into your **README.md** file on GitHub. Replace **"Your Name"** and repository link with your own details.

