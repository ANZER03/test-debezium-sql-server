# =============================================================================
# producer.py — CDC Data Producer (writes changes to SQL Server)
# =============================================================================
# This script connects to SQL Server and performs INSERT, UPDATE, and DELETE
# operations on the AdventureWorks2019 tables that have CDC enabled.
# Debezium will capture these changes and publish them to Kafka topics.
#
# Usage:
#   python3 producer/producer.py
#
# Prerequisites:
#   - SQL Server container running with AdventureWorks2019 restored
#   - CDC enabled on the 5 target tables
#   - pip install pymssql  (see requirements.txt)
# =============================================================================

import time  # For adding delays between operations
import random  # For generating random test data
import string  # For generating random strings
import pymssql  # Microsoft SQL Server client library for Python
from datetime import datetime  # For timestamps in log messages

# =============================================================================
# Configuration — matches docker-compose.yml settings for sqlserver service
# =============================================================================

# SQL Server hostname — 'localhost' because we connect from the host machine
# through the port mapped in docker-compose.yml (1433:1433)
SQL_SERVER_HOST = "localhost"

# SQL Server port — default SQL Server port, mapped 1:1 in docker-compose.yml
SQL_SERVER_PORT = 1433

# SQL Server login — using the SA (System Administrator) account
# In production, use a dedicated service account with limited permissions
SQL_SERVER_USER = "sa"

# SQL Server password — must match MSSQL_SA_PASSWORD in docker-compose.yml
SQL_SERVER_PASSWORD = "YourStrong!Passw0rd"

# Database name — the AdventureWorks2019 database restored in Step 2
SQL_SERVER_DATABASE = "AdventureWorks2019"

# How long to wait (in seconds) between each DML operation
# Lower values = more CDC events per second; higher = easier to follow in logs
OPERATION_DELAY_SECONDS = 3

# =============================================================================
# Helper functions — generate random test data for each table
# =============================================================================


def random_string(length=8):
    """Generate a random alphanumeric string of given length.
    Used for generating test names, product numbers, etc."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def random_person_type():
    """Return a random PersonType code from AdventureWorks2019.
    SC=Store Contact, IN=Individual, SP=Sales Person,
    EM=Employee, VC=Vendor Contact, GC=General Contact."""
    return random.choice(["SC", "IN", "SP", "EM", "VC", "GC"])


def random_color():
    """Return a random product color (matches AdventureWorks color values)."""
    return random.choice(["Black", "Blue", "Red", "Silver", "White", "Yellow", None])


# =============================================================================
# DML Operations — each function performs one type of change that CDC captures
# =============================================================================


def insert_person(cursor):
    """INSERT a new row into Person.Person table.

    This generates a Debezium CDC event with:
      op: 'c' (create)
      before: null
      after: {the new row data}

    Person.Person requires a BusinessEntityID from Person.BusinessEntity (FK).
    We first insert into BusinessEntity to get a valid ID, then insert the Person.
    """
    # Step 1: Insert into Person.BusinessEntity to generate a new BusinessEntityID
    # BusinessEntity is the parent table — Person.Person has a FK to it
    cursor.execute("""
        INSERT INTO Person.BusinessEntity (rowguid, ModifiedDate) 
        VALUES (NEWID(), GETDATE())
    """)

    # Step 2: Retrieve the auto-generated BusinessEntityID (IDENTITY column)
    cursor.execute("SELECT SCOPE_IDENTITY()")
    row = cursor.fetchone()
    business_entity_id = int(row[0])

    # Step 3: Generate random person data
    first_name = random.choice(
        ["Alice", "Bob", "Charlie", "Diana", "Eve", "Frank", "Grace", "Hank"]
    )
    last_name = random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Davis", "Miller", "Wilson"]
    )
    person_type = random_person_type()

    # Step 4: Insert the person row — this is the change that Debezium captures via CDC
    cursor.execute(
        """
        INSERT INTO Person.Person 
            (BusinessEntityID, PersonType, NameStyle, FirstName, LastName, EmailPromotion, rowguid, ModifiedDate)
        VALUES 
            (%d, %s, 0, %s, %s, %d, NEWID(), GETDATE())
    """,
        (business_entity_id, person_type, first_name, last_name, random.randint(0, 2)),
    )

    print(
        f"  [INSERT] Person.Person: BusinessEntityID={business_entity_id}, "
        f"Name={first_name} {last_name}, Type={person_type}"
    )

    # Return the ID so we can use it for related operations later
    return business_entity_id


def update_person(cursor):
    """UPDATE a random existing row in Person.Person table.

    This generates a Debezium CDC event with:
      op: 'u' (update)
      before: {old row data}
      after:  {new row data}

    We pick a random existing person and change their EmailPromotion value.
    """
    # Pick a random person from the top 1000 rows (for performance)
    cursor.execute("""
        SELECT TOP 1 BusinessEntityID, FirstName, LastName 
        FROM Person.Person 
        ORDER BY NEWID()
    """)
    row = cursor.fetchone()

    if row is None:
        print("  [UPDATE] Person.Person: No rows found to update — skipping")
        return

    # Extract the person's data
    beid = row[0]
    first_name = row[1]
    last_name = row[2]

    # Generate a new EmailPromotion value (0=no emails, 1=from AW, 2=from AW+partners)
    new_email_promo = random.randint(0, 2)

    # Perform the UPDATE — Debezium will capture old and new values via CDC
    cursor.execute(
        """
        UPDATE Person.Person 
        SET EmailPromotion = %d, ModifiedDate = GETDATE() 
        WHERE BusinessEntityID = %d
    """,
        (new_email_promo, beid),
    )

    print(
        f"  [UPDATE] Person.Person: BusinessEntityID={beid}, "
        f"Name={first_name} {last_name}, EmailPromotion -> {new_email_promo}"
    )


def insert_product(cursor):
    """INSERT a new row into Production.Product table.

    This generates a Debezium CDC event with:
      op: 'c' (create)
      before: null
      after: {the new product data}
    """
    # Generate a unique product number (AW convention: XX-XXXX)
    product_number = f"ZZ-{random_string(4).upper()}"

    # Generate a random product name
    adjective = random.choice(["Ultra", "Pro", "Elite", "Turbo", "Mega", "Super"])
    noun = random.choice(
        ["Widget", "Gadget", "Sprocket", "Bracket", "Flange", "Bearing"]
    )
    name = f"{adjective} {noun} {random.randint(100, 999)}"

    # Random pricing
    standard_cost = round(random.uniform(10.0, 500.0), 4)
    list_price = round(standard_cost * random.uniform(1.5, 3.0), 4)
    color = random_color()

    # Insert the product — CDC will capture this as a 'create' event
    cursor.execute(
        """
        INSERT INTO Production.Product 
            (Name, ProductNumber, MakeFlag, FinishedGoodsFlag, Color,
             SafetyStockLevel, ReorderPoint, StandardCost, ListPrice,
             DaysToManufacture, SellStartDate, rowguid, ModifiedDate)
        VALUES 
            (%s, %s, 1, 1, %s, %d, %d, %s, %s, %d, GETDATE(), NEWID(), GETDATE())
    """,
        (
            name,
            product_number,
            color,
            random.randint(100, 1000),  # SafetyStockLevel
            random.randint(50, 500),  # ReorderPoint
            str(standard_cost),  # StandardCost as string for decimal precision
            str(list_price),  # ListPrice as string for decimal precision
            random.randint(0, 5),
        ),
    )  # DaysToManufacture

    # Get the auto-generated ProductID
    cursor.execute("SELECT SCOPE_IDENTITY()")
    product_id = int(cursor.fetchone()[0])

    print(
        f"  [INSERT] Production.Product: ProductID={product_id}, "
        f"Name={name}, Price=${list_price:.2f}, Color={color}"
    )

    return product_id


def update_product(cursor):
    """UPDATE a random existing product's ListPrice.

    This generates a Debezium CDC event with:
      op: 'u' (update)
      before: {old product data}
      after:  {new product data with updated price}
    """
    # Pick a random product
    cursor.execute("""
        SELECT TOP 1 ProductID, Name, ListPrice 
        FROM Production.Product 
        ORDER BY NEWID()
    """)
    row = cursor.fetchone()

    if row is None:
        print("  [UPDATE] Production.Product: No rows found — skipping")
        return

    product_id = row[0]
    name = row[1]
    old_price = row[2]

    # Change the price by a random percentage (-20% to +30%)
    price_change = random.uniform(0.8, 1.3)
    new_price = round(float(old_price) * price_change, 4)

    # Perform the update — CDC captures old vs new price
    cursor.execute(
        """
        UPDATE Production.Product 
        SET ListPrice = %s, ModifiedDate = GETDATE() 
        WHERE ProductID = %d
    """,
        (str(new_price), product_id),
    )

    print(
        f"  [UPDATE] Production.Product: ProductID={product_id}, "
        f"Name={name}, Price ${old_price} -> ${new_price:.2f}"
    )


def delete_person(cursor):
    """DELETE a recently inserted person (one we created, not original AW data).

    This generates a Debezium CDC event with:
      op: 'd' (delete)
      before: {the deleted row data}
      after: null

    We only delete persons with BusinessEntityID > 20777 (the max in the original
    AW2019 dataset) to avoid breaking referential integrity on original data.
    """
    # Find a recently inserted person (one we created) that has no dependent records
    # BusinessEntityID > 20777 means it was inserted by us, not part of original data
    cursor.execute("""
        SELECT TOP 1 p.BusinessEntityID, p.FirstName, p.LastName 
        FROM Person.Person p
        WHERE p.BusinessEntityID > 20777
          AND NOT EXISTS (SELECT 1 FROM Sales.Customer c WHERE c.PersonID = p.BusinessEntityID)
        ORDER BY p.BusinessEntityID DESC
    """)
    row = cursor.fetchone()

    if row is None:
        print(
            "  [DELETE] Person.Person: No deletable rows found (need to insert some first) — skipping"
        )
        return

    beid = row[0]
    first_name = row[1]
    last_name = row[2]

    # Delete the person row — CDC captures this as a 'delete' event
    cursor.execute("DELETE FROM Person.Person WHERE BusinessEntityID = %d", (beid,))

    # Also clean up the parent BusinessEntity row to avoid orphans
    cursor.execute(
        "DELETE FROM Person.BusinessEntity WHERE BusinessEntityID = %d", (beid,)
    )

    print(
        f"  [DELETE] Person.Person: BusinessEntityID={beid}, "
        f"Name={first_name} {last_name}"
    )


# =============================================================================
# Main loop — continuously generates DML operations for CDC to capture
# =============================================================================


def main():
    """Main entry point: connects to SQL Server and runs a continuous loop
    of random INSERT/UPDATE/DELETE operations across the CDC-enabled tables.

    Each operation triggers a CDC change that Debezium captures and publishes
    to the corresponding Kafka topic (e.g., aw.AdventureWorks2019.Person.Person).
    """
    print("=" * 60)
    print(" CDC Data Producer — SQL Server DML Generator")
    print("=" * 60)
    print(f" Server:   {SQL_SERVER_HOST}:{SQL_SERVER_PORT}")
    print(f" Database: {SQL_SERVER_DATABASE}")
    print(f" Delay:    {OPERATION_DELAY_SECONDS}s between operations")
    print("=" * 60)
    print()

    # Connect to SQL Server using pymssql
    # pymssql is a Python DB-API 2.0 compliant interface to SQL Server
    print("Connecting to SQL Server...")
    conn = pymssql.connect(
        server=SQL_SERVER_HOST,  # Hostname (localhost since port is mapped)
        port=SQL_SERVER_PORT,  # Port (1433 mapped from Docker)
        user=SQL_SERVER_USER,  # SA account
        password=SQL_SERVER_PASSWORD,
        database=SQL_SERVER_DATABASE,
        autocommit=True,  # Auto-commit each statement so CDC captures it immediately
    )
    cursor = conn.cursor()
    print("Connected successfully!\n")

    # Define the weighted list of operations
    # More inserts and updates than deletes (realistic write pattern)
    operations = [
        ("insert_person", insert_person, 30),  # 30% chance — INSERT into Person.Person
        ("update_person", update_person, 25),  # 25% chance — UPDATE Person.Person
        (
            "insert_product",
            insert_product,
            20,
        ),  # 20% chance — INSERT into Production.Product
        (
            "update_product",
            update_product,
            15,
        ),  # 15% chance — UPDATE Production.Product
        ("delete_person", delete_person, 10),  # 10% chance — DELETE from Person.Person
    ]

    # Build a weighted choices list: each operation appears N times (N = weight)
    weighted_ops = []
    for name, func, weight in operations:
        weighted_ops.extend([(name, func)] * weight)

    # Counter for tracking how many operations we've performed
    op_count = 0

    try:
        print("Starting continuous DML operations (Ctrl+C to stop)...\n")
        while True:
            # Pick a random operation based on weights
            op_name, op_func = random.choice(weighted_ops)
            op_count += 1

            # Print a header for each operation with timestamp
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] Operation #{op_count}: {op_name}")

            try:
                # Execute the chosen DML operation
                op_func(cursor)
            except pymssql.Error as e:
                # If a SQL error occurs, log it and continue with the next operation
                # Common errors: unique constraint violations, FK violations, etc.
                print(f"  [ERROR] SQL error during {op_name}: {e}")

            print()  # Blank line between operations for readability

            # Wait before the next operation
            time.sleep(OPERATION_DELAY_SECONDS)

    except KeyboardInterrupt:
        # Graceful shutdown on Ctrl+C
        print(f"\n{'=' * 60}")
        print(f" Stopping producer. Total operations performed: {op_count}")
        print(f"{'=' * 60}")
    finally:
        # Always close the database connection to free resources
        cursor.close()
        conn.close()
        print("Database connection closed.")


# Standard Python entry point guard
if __name__ == "__main__":
    main()
