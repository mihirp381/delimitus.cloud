from ssc_app import data


def main() -> None:
    rows = data.query("sales-db", "select id, total from orders limit 5").rows
    print(len(rows))


if __name__ == "__main__":
    main()
