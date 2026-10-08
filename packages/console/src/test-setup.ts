// Every test file starts from an empty document, without having to remember to clean up.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup);
